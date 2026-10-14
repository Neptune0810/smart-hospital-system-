import hashlib,json,time,uuid,copy
from .business_state import Delivery
from .guard import MotionGuard

ACTIVE={'pending','running','canceling'}
TERMINAL={'succeeded','failed','canceled'}
ALIASES={'物资库':'depot','A护士站':'station_a','A站':'station_a','B护士站':'station_b','B站':'station_b','C护士站':'station_c','C站':'station_c'}

class MHSDelivery(Delivery):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.guard=MotionGuard(self.guard_stop)
        self.s.setdefault('mhs_budget',None); self.s.setdefault('receiver_unavailable_since',{})
        self.s.setdefault('navigation_tasks',{})
        self.s.setdefault('planning_version',0)
    def event(self,kind,**data):
        if kind in {'orders_created','receiver_changed','order_deferred','order_failed','navigation_failed','handoff_rejected','operator_stopped'}:
            self.s['planning_version']=self.s.get('planning_version',0)+1
        return super().event(kind,**data)
    def claim_session(self,session_key,expected_run_id=None):
        with self.lock:
            if expected_run_id and expected_run_id!=self.s['run_id']:raise ValueError('session_run_mismatch')
            if not isinstance(session_key,str) or not 1<=len(session_key)<=200:raise ValueError('session_identity_required')
            owner=self.s.get('controller_session')
            if owner and owner!=session_key:raise ValueError('another_session_controls_this_run')
            self.s['controller_session']=session_key;self.save()
            return {'run_id':self.s['run_id'],'controller_session':session_key}
    def stop_run(self,operation_id,expected_run_id):
        with self.lock:
            if expected_run_id!=self.s['run_id']:raise ValueError('session_run_mismatch')
            if not operation_id:raise ValueError('operation_id_required')
            if not self.s.get('operator_stopped'):
                self.s['operator_stopped']={'operation_id':operation_id,'wall_time':time.time(),'sim_time':self.s['sim_time']}
                self.event('operator_stopped',operation_id=operation_id)
                b=self.s.get('mhs_budget')
                if b and not b.get('end_wall'):b.update(end_wall=time.time(),end_sim=self.s['sim_time'],end_path=self.s['path_length'])
                self.save()
            result=self.request_stop('operator_stopped_run')
            (self.data_dir/self.s['run_id']/'report.json').write_text(json.dumps(self.verify(),ensure_ascii=False,indent=2))
            return result
    def plan_view(self):
        plan=copy.deepcopy(self.s.get('delivery_plan'))
        if not plan:return None
        plan['stale']=plan['planning_version']!=self.s.get('planning_version',0)
        current_found=False
        for index,step in enumerate(plan['steps']):
            orders=[self.s['orders'][i] for i in step['order_ids']]
            action=step['action'];nav=self.nav
            if action=='pickup':done=bool(orders) and all(o['loaded'] or o['status']=='delivered' for o in orders)
            elif action=='deliver':done=bool(orders) and all(o.get('receipt') for o in orders)
            elif action=='return':
                later_pickup=next((item for item in plan['steps'][index+1:] if item['action']=='pickup'),None)
                pickup_proof=bool(later_pickup and later_pickup['order_ids']) and all(self.s['orders'][i]['loaded'] or self.s['orders'][i].get('receipt') for i in later_pickup['order_ids'])
                done=self.finished() if index==len(plan['steps'])-1 else (not current_found and self.at('depot') and self.stopped()) or pickup_proof
            else:done=self.s['receivers'].get(step['station_id']) is True
            failed=bool(orders) and any(o['status']=='failed' for o in orders)
            moving=bool(nav and nav['status'] in ACTIVE and nav['goal_station']==step['station_id'])
            handoff=action=='deliver' and self.at(step['station_id']) and any(o['loaded'] for o in orders)
            is_current=not current_found and not done and not failed
            if is_current:current_found=True
            step['status']='completed' if done else 'failed' if failed else 'running' if is_current and (moving or handoff) else 'planned'
        return plan
    def stopped(self):
        p=self.pose
        return bool(p and time.time()-self.last_pose_wall<5 and p['speed']<.05 and abs(p.get('angular_speed',0))<.1 and
                    self.still_since is not None and self.s['sim_time']-self.still_since>=1 and
                    (not self.nav or self.nav['status'] in TERMINAL))
    def fresh(self):
        return self.physical_pose() is not None and time.time()-self.last_pose_wall<5
    def guard_stop(self,reason):
        if self.backend:
            self.backend.zero()
            if self.nav and self.nav['status'] in ACTIVE:
                self.nav['status']='canceling';self.backend.cancel()
        self.event('motion_guard_stop',reason=reason)
    def request_stop(self,reason='operator_stop'):
        with self.lock:
            self.guard.trip(reason)
            self.guard_stop(reason)
            return {'requested':True,'confirmed':self.stopped(),'guard':self.guard.state()}
    def state(self,since_event_id=0):
        with self.lock:
            s=super().state(since_event_id)
            if hasattr(self,'guard'):
                s['motion_guard']=self.guard.state();s['stopped_confirmed']=self.stopped();s['pose_wall_time']=self.last_pose_wall
                s['odometry']=getattr(self.backend,'odometry',None)
                s['delivery_plan']=self.plan_view()
            return copy.deepcopy(s)
    def receiver(self,station,available):
        super().receiver(station,available)
        timers=self.s.setdefault('receiver_unavailable_since',{})
        if available:timers.pop(station,None)
        else:timers.setdefault(station,self.s['sim_time'])
        self.save()
    def create_delivery_orders(self,items,operation_id):
        if not isinstance(operation_id,str) or not operation_id:raise ValueError('operation_id_required')
        if not isinstance(items,list) or not 1<=len(items)<=20:raise ValueError('invalid_order_batch')
        op=self.s['run_id']+':'+operation_id
        # Resolve the relative deadline once, including after a transport retry.
        sig=json.dumps(['create_delivery_orders',items],sort_keys=True)
        with self.lock:
            row=self.db.execute('SELECT signature,body FROM operations WHERE id=?',(op,)).fetchone()
            if row:
                if row[0]!=sig:raise ValueError('operation_id_conflict')
                stored=json.loads(row[1])
                if stored.get('_rejected'):raise ValueError(stored['error'])
                return stored
            if self.s.get('mhs_finished'):raise ValueError('new_run_required_after_completion')
            orders=[]
            for i,v in enumerate(items):
                station=ALIASES.get(v['destination'],v['destination']);priority=v.get('priority','normal')
                seconds=v.get('deadline_seconds')
                if seconds is None:seconds=100 if priority=='urgent' else 240
                if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not 0<seconds<=1200:raise ValueError('invalid_deadline_seconds')
                ident=hashlib.sha256((op+':'+str(i)).encode()).hexdigest()[:12]
                orders.append(dict(order_id='O-'+ident,package_id='P-'+ident,pickup_station='depot',dropoff_station=station,
                    priority=priority,deadline_sim=self.s['sim_time']+seconds,package_label=v.get('package_label','')))
            try:result=self.mutate('create_orders',{'orders':orders})
            except ValueError as error:
                self.db.execute('INSERT INTO operations VALUES (?,?,?)',(op,sig,json.dumps({'_rejected':True,'error':str(error)})));self.db.commit()
                raise
            self.save()
            self.db.execute('INSERT INTO operations VALUES (?,?,?)',(op,sig,json.dumps(result)));self.db.commit()
            return result
    def do(self,name,args):
        op=args.get('operation_id')
        key=self.s['run_id']+':'+op if isinstance(op,str) and op else None
        sig=json.dumps([name,args],sort_keys=True)
        with self.lock:
            if key:
                row=self.db.execute('SELECT signature,body FROM operations WHERE id=?',(key,)).fetchone()
                if row and json.loads(row[1]).get('_rejected'):
                    if row[0]!=sig:raise ValueError('operation_id_conflict')
                    raise ValueError(json.loads(row[1])['error'])
            try:return super().do(name,args)
            except ValueError as e:
                if key and str(e)!='operation_id_conflict':
                    self.save()
                    self.db.execute('INSERT OR IGNORE INTO operations VALUES (?,?,?)',(key,sig,json.dumps({'_rejected':True,'error':str(e)})));self.db.commit()
                raise
    def mutate(self,name,args):
        if self.s.get('operator_stopped') and name not in {'publish_delivery_plan'}:raise ValueError('run_stopped_by_operator_new_round_required')
        if name=='publish_delivery_plan':
            steps=args.get('steps');reason=args.get('reason');version=args.get('planning_version')
            if isinstance(version,bool) or version!=self.s.get('planning_version',0):raise ValueError('plan_state_changed_read_latest_state')
            if not isinstance(reason,str) or not 1<=len(reason.strip())<=180:raise ValueError('public_reason_required_max_180')
            if not isinstance(steps,list) or not 1<=len(steps)<=20:raise ValueError('invalid_plan_steps')
            normalized=[]
            for step in steps:
                action=step.get('action');station=step.get('station_id');ids=step.get('order_ids',[])
                if action not in {'pickup','deliver','wait','return'} or station not in self.stations:raise ValueError('invalid_plan_action_or_station')
                if not isinstance(ids,list) or len(set(ids))!=len(ids) or any(i not in self.s['orders'] for i in ids):raise ValueError('unknown_plan_order')
                if action in {'pickup','deliver','wait'} and not ids:raise ValueError('plan_order_ids_required')
                if action in {'pickup','return'} and station!='depot':raise ValueError('plan_depot_required')
                if action in {'deliver','wait'} and any(self.s['orders'][i]['dropoff_station']!=station for i in ids):raise ValueError('plan_order_destination_mismatch')
                normalized.append({'action':action,'station_id':station,'order_ids':ids})
            plan={'steps':normalized,'reason':reason.strip(),'planning_version':version,'wall_time':time.time(),'sim_time':self.s['sim_time'],'operation_id':args['operation_id']}
            self.s['delivery_plan']=plan;self.event('delivery_plan_published',plan=plan)
            return {'ok':True,'plan':plan}
        b=self.s.get('mhs_budget')
        if b and b.get('stopped_reason') and name in {'create_orders','pickup_package','navigate_to','confirm_delivery','defer_order'}:
            raise ValueError('run_budget_exhausted')
        if name=='navigate_to':
            station=args.get('station_id')
            active_orders=[o for o in self.s['orders'].values() if o['status'] not in {'delivered','failed'}]
            if station in self.stations and station!='depot' and active_orders and not any(o['loaded'] and o['dropoff_station']==station for o in active_orders):
                raise ValueError('pickup_required_before_delivery_navigation')
            # A duplicate is handled by Delivery.do before this gate.
            self.guard.arm(self.stopped(),self.fresh())
            try:
                result=super().mutate(name,args)
                self.s['navigation_tasks'][self.nav['nav_task_id']]=dict(self.nav)
                return result
            except Exception:
                self.guard.trip('navigation_rejected');raise
        if name=='confirm_delivery':
            order=self.s['orders'].get(args.get('order_id'))
            if order and args.get('package_id')==order['package_id'] and order.get('loaded') and self.at(order['dropoff_station']) and not self.s['receivers'][order['dropoff_station']]:
                if order['attempts']>=2:raise ValueError('handoff_retry_limit')
                order['attempts']+=1;self.event('handoff_rejected',order_id=order['order_id'],reason='receiver_unavailable',attempt=order['attempts']);self.save()
                raise ValueError('receiver_unavailable')
        r=super().mutate(name,args)
        if name=='create_orders' and not self.s.get('mhs_budget'):
            self.s['mhs_budget']=dict(start_sim=self.s['sim_time'],start_wall=time.time(),start_path=self.s['path_length'],decisions=0,stopped_reason=None)
            self.event('mhs_task_started')
        return r
    def decision(self,decision_id,expected_run_id=None,session_key=None):
        with self.lock:
            if expected_run_id is not None and expected_run_id!=self.s['run_id']:raise ValueError('session_run_mismatch')
            if session_key:self.claim_session(session_key,expected_run_id)
            if self.s.get('operator_stopped'):raise ValueError('run_stopped_by_operator_new_round_required')
            b=self.s.get('mhs_budget')
            if b and not b.get('end_wall') and not self.s.get('mhs_finished') and not self.finished():
                previous=self.s.setdefault('decision_ids',[])
                if decision_id not in previous:previous.append(decision_id);b['decisions']+=1;self.save()
            return {'ok':True,'budget':b,'run_id':self.s['run_id']}
    def nav_result(self,tid,status,reason=None):
        with self.lock:
            super().nav_result(tid,status,reason)
            if self.nav and self.nav['nav_task_id']==tid:
                self.s.setdefault('navigation_tasks',{})[tid]=dict(self.nav)
                if status in TERMINAL:self.guard.trip('navigation_'+status)
                self.save()
    def finished(self):
        return bool(self.s['orders']) and all(o['status'] in {'delivered','failed'} for o in self.s['orders'].values()) and self.at('depot') and self.stopped()
    def tick(self):
        with self.lock:
            self.guard.tick(self.fresh())
            b=self.s.get('mhs_budget')
            # Safety telemetry stays live; stopped task outcomes are frozen.
            if self.s.get('operator_stopped') or (b or {}).get('stopped_reason'):
                return
            if b and not b.get('end_wall') and not self.s.get('mhs_finished') and not b.get('stopped_reason') and not self.finished():
                reason='decision_limit' if b['decisions']>=60 else 'sim_time_limit' if self.s['sim_time']-b['start_sim']>=2400 else 'wall_time_limit' if time.time()-b['start_wall']>=3600 else None
                if reason:
                    b['stopped_reason']=reason;self.request_stop(reason);self.event('mhs_budget_exhausted',reason=reason);self.save()
                    return
            for o in self.s['orders'].values():
                since=self.s.get('receiver_unavailable_since',{}).get(o['dropoff_station'])
                if o['status'] not in {'delivered','failed'} and since is not None and self.s['sim_time']-since>=150:
                    super().mutate('close_order',dict(order_id=o['order_id'],reason='receiver_unavailable_150_sim_seconds'));self.save()
            if self.finished() and not self.s.get('mhs_finished'):
                if b:b.update(end_sim=self.s['sim_time'],end_wall=time.time(),end_path=self.s['path_length'])
                self.s['mhs_finished']=True;self.event('mhs_task_completed');self.save()
                (self.data_dir/self.s['run_id']/'report.json').write_text(json.dumps(self.verify(),ensure_ascii=False,indent=2))
    def reset(self):
        with self.lock:
            super().reset();self.s['mhs_budget']=None;self.s['receiver_unavailable_since']={};self.s['navigation_tasks']={};self.guard.trip('scene_reset');self.save();return self.state()
    def verify(self,run_id=None):
        r=super().verify(run_id);b=self.s.get('mhs_budget')
        r.update(returned_to_depot=self.at('depot') and self.stopped(),mhs_budget=b,operator_stopped=self.s.get('operator_stopped'),delivery_plan=self.plan_view())
        if b:
            r.update(wall_seconds=b.get('end_wall',time.time())-b['start_wall'],
                elapsed_sim=b.get('end_sim',self.s['sim_time'])-b['start_sim'],
                path_length=b.get('end_path',self.s['path_length'])-b.get('start_path',0))
        return r
