import json, sqlite3, time, threading, uuid, math, os
from pathlib import Path

class Delivery:
    def __init__(self, root, stations, require_sim_truth=False, data_dir=None):
        self.root=Path(root); self.stations=stations; self.require_sim_truth=require_sim_truth; self.lock=threading.RLock()
        self.data_dir=Path(data_dir) if data_dir is not None else self.root/'runs/mhs-v2'
        self.data_dir.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.data_dir/'state.sqlite',check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, body TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, body TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, signature TEXT, body TEXT)')
        row=self.db.execute('SELECT body FROM snapshots WHERE id=1').fetchone()
        self.s=json.loads(row[0]) if row else self.new_state()
        self.pose=None; self.nav=None; self.last_pose_wall=0; self.still_since=None
        self.backend=None; self.started_wall=self.s.get('start_wall',time.time())
        self.save()
    def new_state(self):
        return {'run_id':uuid.uuid4().hex,'orders':{},'receivers':{k:True for k in self.stations},
                'capacity':2,'sim_time':0,'start_wall':time.time(),'path_length':0,'trajectory':[], 'mode':'live-nav2'}
    def save(self):
        self.db.execute('INSERT OR REPLACE INTO snapshots VALUES (1,?)',(json.dumps(self.s),));self.db.commit()
        folder=self.data_dir/self.s['run_id'];folder.mkdir(exist_ok=True)
        tmp=folder/'state.tmp';tmp.write_text(json.dumps(self.s));tmp.replace(folder/'state.json')
    def event(self,kind,**data):
        body=dict(kind=kind,wall_time=time.time(),sim_time=self.s['sim_time'],**data)
        cur=self.db.execute('INSERT INTO events(run_id,body) VALUES (?,?)',(self.s['run_id'],json.dumps(body)))
        self.db.commit();body['event_id']=cur.lastrowid
        p=self.data_dir/self.s['run_id'];p.mkdir(exist_ok=True)
        with (p/'events.jsonl').open('a') as f:f.write(json.dumps(body)+'\n')
        return body
    def telemetry(self,x,y,yaw,speed,sim_time,angular_speed=0):
        with self.lock:
            if self.pose:
                d=math.hypot(x-self.pose['x'],y-self.pose['y'])
                if d<2:self.s['path_length']+=d
            self.pose=dict(x=x,y=y,yaw=yaw,speed=speed,angular_speed=angular_speed);self.last_pose_wall=time.time();self.s['sim_time']=sim_time
            if 'start_sim' not in self.s:self.s['start_sim']=sim_time
            if speed<0.05 and abs(angular_speed)<0.1:
                if self.still_since is None:self.still_since=sim_time
            else:self.still_since=None
            if not self.s['trajectory'] or sim_time-self.s['trajectory'][-1][0]>=0.5:
                self.s['trajectory'].append([sim_time,x,y]);self.save()
    def physical_pose(self):
        try:
            v=json.loads((self.root/'runs/physical-pose.json').read_text())
            return v if time.time()-v['wall_time']<5 else None
        except (OSError,ValueError,KeyError):return None
    def at(self,station):
        if not self.pose or time.time()-self.last_pose_wall>5:return False
        t=self.stations[station]
        if self.require_sim_truth:
            truth=self.physical_pose()
            if not truth or math.hypot(truth['position'][0]-t['x'],truth['position'][1]-t['y'])>0.5:return False
        return math.hypot(self.pose['x']-t['x'],self.pose['y']-t['y'])<=0.5 and self.pose['speed']<0.05 and abs(self.pose.get('angular_speed',0))<0.1 and self.still_since is not None and self.s['sim_time']-self.still_since>=1
    def state(self,since_event_id=0):
        with self.lock:
            rows=list(self.db.execute('SELECT id,body FROM events WHERE run_id=? AND id>? ORDER BY id DESC LIMIT 100',(self.s['run_id'],since_event_id)))
            ev=[dict(event_id=i,**json.loads(b)) for i,b in reversed(rows)]
            return dict(**self.s,stations=self.stations,pose=self.pose,physical_pose=self.physical_pose(),pose_fresh=time.time()-self.last_pose_wall<5,
                        navigation=self.nav,map={'origin':[-49.625,-4.675],'resolution':0.05},events=ev,latest_event_id=self.db.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE run_id=?',(self.s['run_id'],)).fetchone()[0],
                        navigation_ready=bool(self.backend and self.backend.ready()),at_stations=[k for k in self.stations if self.at(k)])
    def do(self,name,args):
        with self.lock:
            if name=='get_state':return self.state(args.get('since_event_id',0))
            if name=='verify_run':return self.verify(args.get('run_id'))
            op=args.get('operation_id')
            if not isinstance(op,str) or not op:raise ValueError('operation_id_required')
            op=self.s['run_id']+':'+op
            sig=json.dumps([name,args],sort_keys=True)
            row=self.db.execute('SELECT signature,body FROM operations WHERE id=?',(op,)).fetchone()
            if row:
                if row[0]!=sig:raise ValueError('operation_id_conflict')
                return json.loads(row[1])
            result=self.mutate(name,args)
            self.save()
            self.db.execute('INSERT INTO operations VALUES (?,?,?)',(op,sig,json.dumps(result)));self.db.commit()
            return result
    def mutate(self,name,a):
        if name=='create_orders':
            items=a['orders']; pending=[]; seen_o=set(self.s['orders']);seen_p={o['package_id'] for o in self.s['orders'].values()}
            if not isinstance(items,list) or not 1<=len(items)<=20:raise ValueError('invalid_order_batch')
            for o in items:
                required=['order_id','package_id','pickup_station','dropoff_station','priority','deadline_sim']
                if any(k not in o for k in required):raise ValueError('missing_order_fields')
                if not isinstance(o['order_id'],str) or not o['order_id'] or not isinstance(o['package_id'],str) or not o['package_id']:raise ValueError('invalid_id')
                if o['order_id'] in seen_o or o['package_id'] in seen_p:raise ValueError('duplicate_order_or_package')
                if o['pickup_station'] not in self.stations or o['dropoff_station'] not in self.stations or o['pickup_station']==o['dropoff_station']:raise ValueError('invalid_station')
                if o['priority'] not in ['normal','urgent'] or not isinstance(o['deadline_sim'],(int,float)) or not math.isfinite(o['deadline_sim']) or o['deadline_sim']<=self.s['sim_time']:raise ValueError('invalid_priority_or_deadline')
                seen_o.add(o['order_id']);seen_p.add(o['package_id'])
                pending.append(dict(o,status='pending',created_at_sim=self.s['sim_time'],loaded=False,attempts=0))
            for o in pending:self.s['orders'][o['order_id']]=o
            self.event('orders_created',orders=pending);return {'ok':True,'orders':pending}
        if name=='navigate_to':
            station=a['station_id']
            if station not in self.stations:raise ValueError('invalid_station')
            if self.nav and self.nav['status'] in ['running','canceling','pending']:raise ValueError('navigation_busy')
            if not self.backend or not self.backend.ready():raise ValueError('navigation_unavailable')
            self.nav=dict(nav_task_id=uuid.uuid4().hex,goal_station=station,status='pending',started_at_sim=self.s['sim_time'])
            self.backend.start(self.nav['nav_task_id'],self.stations[station])
            self.event('navigation_started',navigation=dict(self.nav));return dict(self.nav)
        if name=='cancel_navigation':
            if not self.nav or a['nav_task_id']!=self.nav['nav_task_id']:raise ValueError('unknown_navigation')
            if self.nav['status'] in ['pending','running']:
                self.nav['status']='canceling';self.backend.cancel()
                self.event('navigation_cancel_requested',nav_task_id=a['nav_task_id'])
            return dict(self.nav)
        if name not in ['pickup_package','confirm_delivery','defer_order','close_order']:raise ValueError('unknown_tool')
        o=self.s['orders'].get(a['order_id'])
        if not o:raise ValueError('unknown_order')
        if name=='pickup_package':
            if o['status'] in ['delivered','failed']:raise ValueError('order_terminal')
            if o['loaded']:return {'ok':True,'already_loaded':True,'order':o}
            if not self.at(o['pickup_station']):raise ValueError('not_at_pickup')
            if sum(x['loaded'] for x in self.s['orders'].values())>=self.s['capacity']:raise ValueError('capacity_exceeded')
            o.update(status='onboard',loaded=True);self.event('package_picked_up',order_id=o['order_id'])
        elif name=='confirm_delivery':
            if a.get('package_id')!=o['package_id']:raise ValueError('package_mismatch')
            if o['status']=='delivered':return {'ok':True,'already_delivered':True,'receipt':o['receipt']}
            if not o['loaded'] or o['status']=='failed':raise ValueError('package_not_onboard')
            if not self.at(o['dropoff_station']):raise ValueError('not_at_destination')
            if not self.s['receivers'][o['dropoff_station']]:raise ValueError('receiver_unavailable')
            receipt=dict(receipt_id=uuid.uuid4().hex,order_id=o['order_id'],package_id=o['package_id'],station=o['dropoff_station'],sim_time=self.s['sim_time'],simulated_handoff=True,arrival_pose=dict(self.pose),physical_pose=self.physical_pose(),pose_wall_time=self.last_pose_wall)
            o.update(status='delivered',loaded=False,receipt=receipt)
            self.event('delivery_confirmed',receipt=receipt)
        elif name=='defer_order':
            if o['status'] in ['delivered','failed']:raise ValueError('order_terminal')
            if not isinstance(a.get('reason'),str) or not a['reason']:raise ValueError('reason_required')
            if not isinstance(a.get('retry_after_sim'),(int,float)) or not math.isfinite(a['retry_after_sim']) or a['retry_after_sim']<=self.s['sim_time']:raise ValueError('invalid_retry_time')
            o.update(status='deferred',reason=a['reason'],retry_after_sim=a['retry_after_sim'])
            self.event('order_deferred',order_id=o['order_id'],reason=a['reason'])
        else:
            if o['status']=='delivered':raise ValueError('cannot_fail_delivered_order')
            o.update(status='failed',reason=a['reason']);self.event('order_failed',order_id=o['order_id'],reason=a['reason'])
        return {'ok':True,'order':dict(o)}
    def nav_result(self,tid,status,reason=None):
        with self.lock:
            if self.nav and self.nav['nav_task_id']==tid:
                self.nav.update(status=status,reason=reason);self.event('navigation_'+status,nav_task_id=tid,reason=reason,pose=self.pose,physical_pose=self.physical_pose())
    def receiver(self,station,available):
        with self.lock:
            if station not in self.stations:raise ValueError('invalid_station')
            self.s['receivers'][station]=bool(available);self.event('receiver_changed',station=station,available=bool(available));self.save()
    def reset(self):
        with self.lock:
            if self.nav and self.nav['status'] in ['pending','running','canceling']:raise ValueError('cancel_navigation_first')
            folder=self.data_dir/self.s['run_id'];folder.mkdir(exist_ok=True)
            # A completed run snapshot must survive later simulator clock resets.
            if not (folder/'final-state.json').exists():(folder/'final-state.json').write_text(json.dumps(self.state()))
            if not (folder/'report.json').exists():(folder/'report.json').write_text(json.dumps(self.verify()))
            clock=self.s['sim_time'];self.s=self.new_state();self.s.update(sim_time=clock,start_sim=clock);self.nav=None;self.started_wall=time.time();self.save();self.event('run_started');return self.state()
    def verify(self,run_id=None):
        if run_id and run_id!=self.s['run_id']:raise ValueError('unknown_run')
        orders=list(self.s['orders'].values());delivered=[o for o in orders if o['status']=='delivered']
        return dict(run_id=self.s['run_id'],total=len(orders),delivered=len(delivered),on_time=sum(o['receipt']['sim_time']<=o['deadline_sim'] for o in delivered),
                    failed=[o['order_id'] for o in orders if o['status']=='failed'],unfinished=[o['order_id'] for o in orders if o['status'] not in ['delivered','failed']],
                    path_length=self.s['path_length'],wall_seconds=time.time()-self.started_wall,sim_time=self.s['sim_time'],elapsed_sim=self.s['sim_time']-self.s.get('start_sim',self.s['sim_time']),receipts=[o['receipt'] for o in delivered],
                    error_deliveries=sum(r['package_id']!=o['package_id'] or r['station']!=o['dropoff_station'] or math.hypot(r['arrival_pose']['x']-self.stations[r['station']]['x'],r['arrival_pose']['y']-self.stations[r['station']]['y'])>0.5 for o in delivered for r in [o['receipt']]),collisions=None,token_usage=None,source='business_state_and_ROS_localization',simulated_handoff=True)
