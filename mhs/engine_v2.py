"""Point-to-point business and Agent-selected route validation."""
import copy, hashlib, json, math, time, uuid
from .engine import MHSDelivery, ACTIVE, TERMINAL
from .business_state import Delivery
from .routes import Routes
from .route_evidence import EVIDENCE_KEY, route_summary, write_route_evidence
from .proximity import people_gate
from .layout import station_digest, validate_layout

class HospitalV2(MHSDelivery):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.routes=Routes(self.root);self.s.setdefault('closed_zones',[])
        self.s.setdefault('protocol','hospital-v2');self.s.setdefault('segment',0);self.route_query_busy=False
        self.s.setdefault('segment_order_ids',list(self.s['orders']))
        validate_layout(self.stations,self.routes.cfg)
        self.freeze_layout()
    def freeze_layout(self):
        digest=station_digest(self.stations)
        prior=self.s.get('station_layout',{}).get('geometry_sha256')
        if prior!=digest and self.s['orders']:
            raise RuntimeError('layout_change_requires_archived_empty_run')
        if prior!=digest:
            self.s['receivers']={k:self.s['receivers'].get(k,True) for k in self.stations}
            self.s['station_layout']={'version':self.routes.version,'geometry_sha256':digest,'points':copy.deepcopy(self.stations)}
            self.save()
    def resolve(self,name):
        if name in self.stations:return name
        key=str(name).strip().lower().replace(' ','')
        canonical=key.replace('-','_')
        if canonical in self.stations:return canonical
        for ident,p in self.stations.items():
            if key in [str(n).lower().replace(' ','') for n in [p['label'],*p.get('aliases',[])]]:return ident
        raise ValueError('unknown_place_ask_user')
    def tick(self):
        with self.lock:
            super().tick()
            b=self.s.get('mhs_budget')
            if self.s.get('operator_stopped') or (b or {}).get('stopped_reason') or self.s.get('mhs_outcome'):
                return
            orders=[self.s['orders'][ident] for ident in self.s['segment_order_ids']]
            if orders and all(o['status'] in {'delivered','failed'} for o in orders) and any(o['status']=='failed' for o in orders) and self.stopped() and not (self.nav and self.nav['status'] in ACTIVE):
                self.s['mhs_outcome']='incomplete'
                if b:b.update(end_sim=self.s['sim_time'],end_wall=time.time(),end_path=self.s['path_length'])
                self.guard.trip('orders_closed_undelivered')
                self.event('mhs_task_incomplete',failed_order_ids=[o['order_id'] for o in orders if o['status']=='failed'],retained_cargo=sum(o['loaded'] for o in self.s['orders'].values()))
                self.save()
                (self.data_dir/self.s['run_id']/'report.json').write_text(json.dumps(self.verify(),ensure_ascii=False,indent=2))
    def telemetry(self,x,y,yaw,speed,sim_time,angular_speed=0):
        with self.lock:
            previous=self.s['sim_time']
            if sim_time+1<previous:
                active=bool(self.nav and self.nav['status'] in ACTIVE) or any(n['status'] in ACTIVE for n in self.s.get('navigation_tasks',{}).values())
                if not self.s['orders'] and not active:
                    folder=self.data_dir/self.s['run_id'];folder.mkdir(exist_ok=True)
                    (folder/('pre-clock-rebase-'+uuid.uuid4().hex+'.json')).write_text(json.dumps({'previous_sim':previous,'trajectory':self.s['trajectory'],'path_length':self.s['path_length']}))
                    self.s.update(start_sim=sim_time,trajectory=[],path_length=0);self.pose=None;self.still_since=None
                    self.event('scene_clock_rebased',previous_sim=previous,new_sim=sim_time)
                else:
                    b=self.s.get('mhs_budget')
                    if b:b.update(stopped_reason='simulation_clock_regression',end_sim=previous,end_wall=time.time(),end_path=self.s['path_length'])
                    self.request_stop('simulation_clock_regression');self.still_since=None
                    self.event('simulation_clock_regression',previous_sim=previous,new_sim=sim_time)
            super().telemetry(x,y,yaw,speed,sim_time,angular_speed)
    def fresh(self):
        return super().fresh() and (not hasattr(self,'routes') or self.routes.crowd()['fresh'])
    def create_delivery_orders(self,items,operation_id):
        if not isinstance(operation_id,str) or not operation_id:raise ValueError('operation_id_required')
        op=self.s['run_id']+':'+operation_id;signature=json.dumps(['create_delivery_orders',items],sort_keys=True)
        with self.lock:
            row=self.db.execute('SELECT signature,body FROM operations WHERE id=?',(op,)).fetchone()
            if row:
                if row[0]!=signature:raise ValueError('operation_id_conflict')
                result=json.loads(row[1])
                if result.get('_rejected'):raise ValueError(result['error'])
                return result
            try:return self._create_delivery_orders(items,operation_id)
            except ValueError as error:
                self.db.execute('INSERT INTO operations VALUES (?,?,?)',(op,signature,json.dumps({'_rejected':True,'error':str(error)})));self.db.commit()
                raise
    def _create_delivery_orders(self,items,operation_id):
        if not isinstance(operation_id,str) or not operation_id:raise ValueError('operation_id_required')
        if not isinstance(items,list) or not 1<=len(items)<=20:raise ValueError('invalid_order_batch')
        op=self.s['run_id']+':'+operation_id;signature=json.dumps(['create_delivery_orders',items],sort_keys=True)
        with self.lock:
            row=self.db.execute('SELECT signature,body FROM operations WHERE id=?',(op,)).fetchone()
            if row:
                if row[0]!=signature:raise ValueError('operation_id_conflict')
                return json.loads(row[1])
            if self.s.get('operator_stopped'):raise ValueError('run_stopped_by_operator_new_round_required')
            if self.s.get('mhs_outcome')=='incomplete':raise ValueError('new_run_required_after_incomplete')
            orders=[]
            for item in items:
                if not isinstance(item,dict):raise ValueError('invalid_order_item')
                quantity=item.get('quantity',1)
                if isinstance(quantity,bool) or not isinstance(quantity,int) or not 1<=quantity<=20 or len(orders)+quantity>20:raise ValueError('invalid_package_quantity')
                pickup=self.resolve(item.get('pickup_station') or 'depot');dropoff=self.resolve(item['destination'])
                if pickup==dropoff:raise ValueError('pickup_and_destination_must_differ')
                priority=item.get('priority','normal');seconds=item.get('deadline_seconds')
                if priority not in {'normal','urgent'}:raise ValueError('invalid_priority')
                if seconds is None:seconds=300 if priority=='urgent' else 900
                if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or not 0<seconds<=7200:raise ValueError('invalid_deadline_seconds')
                for _ in range(quantity):
                    ident=hashlib.sha256((op+':'+str(len(orders))).encode()).hexdigest()[:12]
                    orders.append(dict(order_id='O-'+ident,package_id='P-'+ident,pickup_station=pickup,dropoff_station=dropoff,priority=priority,deadline_sim=self.s['sim_time']+seconds,package_label=item.get('package_label','')))
            # Invalid submissions cannot reopen or erase a completed segment.
            if self.s.get('mhs_finished'):
                folder=self.data_dir/self.s['run_id'];(folder/f"segment-{self.s['segment']}-report.json").write_text(json.dumps(self.verify()))
                self.s['segment']+=1;self.s['mhs_finished']=False;self.s['mhs_budget']=None;self.s['delivery_plan']=None;self.s['segment_order_ids']=[]
            result=self.mutate('create_orders',{'orders':orders});result['planning_version']=self.s['planning_version']
            self.s['segment_order_ids'].extend(o['order_id'] for o in orders)
            self.save();self.db.execute('INSERT INTO operations VALUES (?,?,?)',(op,signature,json.dumps(result)));self.db.commit();return result
    def route_options(self,station_id):
        station_id=self.resolve(station_id)
        with self.lock:
            if not self.fresh() or not self.backend or not self.backend.ready():raise ValueError('navigation_or_crowd_not_ready')
            if self.nav and self.nav['status'] in ACTIVE or not self.stopped():raise ValueError('route_query_requires_stopped_navigation')
            if self.route_query_busy:raise ValueError('route_query_in_progress')
            self.require_ready_goal(station_id)
            if self.at(station_id) and self.stopped():
                return {'map_version':self.routes.version,'destination':station_id,'already_at_destination':True,'routes':[],'rejected':[],'next_action':'Already stationary at this station; proceed with pickup or handoff, no navigation needed.'}
            pose=dict(self.pose);closed=list(self.s['closed_zones']);target=dict(self.stations[station_id],id=station_id)
            self.route_query_busy=True
        try:return self.compute_route_options(pose,target,closed)
        finally:
            try:
                if hasattr(self.backend,'finish_route_query'):self.backend.finish_route_query()
            finally:
                with self.lock:self.route_query_busy=False
    def compute_route_options(self,pose,target,closed):
        station_id=target['id']
        rows=self.routes.options(pose,target,closed);valid=[];rejected=[]
        if hasattr(self.backend,'prepare_route_query'):self.backend.prepare_route_query()
        for row in rows:
            try:
                self.backend.validate_route(row)
                row['validation']='nav2_verified'
                # Dense Nav2 proof stays in Routes.cache and a separate immutable
                # artifact. State/transactions/events only carry its SHA reference.
                row[EVIDENCE_KEY]=write_route_evidence(self.root,self.data_dir,self.s['run_id'],row)
                valid.append(route_summary(row))
            except Exception as error:
                row['validation']='occupancy_clearance_only'
                rejected.append({'route_id':row['route_id'],'error':str(error)})
        result={'map_version':self.routes.version,'destination':station_id,'routes':valid,'rejected':rejected,'estimated_time_excludes_model_wait':True}
        # SmartWard groups state and event in its existing transaction. Route
        # generation, Nav2 validation and raw evidence writes stay outside it.
        atomic=getattr(self,'_atomic',None)
        with atomic() if callable(atomic) else self.lock:
            self.s['route_options']=valid;self.event('route_options_generated',result=result);self.save()
        return result
    def nav_result(self,tid,status,reason=None):
        with self.lock:
            super().nav_result(tid,status,reason)
            if status in TERMINAL and tid in self.s['navigation_tasks']:
                self.s['navigation_tasks'][tid]['completed_wall']=time.time();self.save()
    def reset(self):
        with self.lock:
            super().reset()
            self.s.update(closed_zones=[],protocol='hospital-v2',segment=0,planning_version=0,segment_order_ids=[])
            self.freeze_layout()
            self.routes.cache.clear();self.save();return self.state()
    def mutate(self,name,a):
        if self.s.get('mhs_outcome')=='incomplete' and name!='publish_delivery_plan':raise ValueError('new_run_required_after_incomplete')
        if name=='navigate_route':
            if self.s.get('operator_stopped') or (self.s.get('mhs_budget') or {}).get('stopped_reason'):raise ValueError('run_stopped')
            if self.route_query_busy:raise ValueError('route_query_in_progress')
            if self.nav and self.nav['status'] in ACTIVE:raise ValueError('navigation_busy_stop_first')
            if any(o['status'] not in {'delivered','failed'} for o in self.s['orders'].values()):
                plan=self.s.get('delivery_plan')
                if not plan or plan['planning_version']!=self.s['planning_version']:raise ValueError('publish_current_delivery_plan_before_motion')
            route=self.routes.selected(a['route_id'],self.s['closed_zones'])
            self.require_ready_goal(route['destination'])
            if math.dist(route['points'][0],[self.pose['x'],self.pose['y']])>1:raise ValueError('route_start_changed_get_new_options')
            self.guard.arm(self.stopped(),self.fresh())
            if not self.backend or not self.backend.ready():self.guard.trip('navigation_unavailable');raise ValueError('navigation_unavailable')
            self.nav={'nav_task_id':uuid.uuid4().hex,'goal_station':route['destination'],'status':'pending','started_at_sim':self.s['sim_time'],'started_wall':time.time(),'route_id':route['route_id']}
            self.s['selected_route']=route_summary(route);self.s['navigation_tasks'][self.nav['nav_task_id']]=dict(self.nav)
            self.backend.start_route(self.nav['nav_task_id'],route)
            self.event('navigation_started',navigation=dict(self.nav),route_id=route['route_id']);return dict(self.nav)
        if name=='navigate_to':raise ValueError('v2_choose_route_first')
        if name=='publish_delivery_plan':
            if a.get('planning_version')!=self.s['planning_version']:raise ValueError('plan_state_changed_read_latest_state')
            reason=a.get('reason','').strip();steps=a.get('steps',[])
            if not 1<=len(reason)<=180 or not 1<=len(steps)<=30:raise ValueError('invalid_plan')
            normalized=[]
            for step in steps:
                step=dict(step);step.pop('arrival_at_publish',None)
                action=step.get('action');station=self.resolve(step['station_id']);ids=step.get('order_ids',[])
                if action not in {'navigate','pickup','deliver','wait','return','standby'}:raise ValueError('invalid_plan_action')
                if len(set(ids))!=len(ids) or any(i not in self.s['orders'] for i in ids):raise ValueError('unknown_plan_order')
                if action in {'pickup','deliver','wait'}:
                    if not ids:raise ValueError('plan_order_ids_required')
                    field='pickup_station' if action=='pickup' else 'dropoff_station'
                    if any(self.s['orders'][i][field]!=station for i in ids):raise ValueError('plan_order_destination_mismatch')
                step.update(station_id=station,order_ids=ids);normalized.append(step)
                if len(normalized)==1 and action=='navigate' and self.at(station) and self.stopped():
                    step['arrival_at_publish']={'source':'validated_station_pose','wall_time':time.time(),'pose':dict(self.pose)}
            if normalized[-1]['action'] not in {'standby','return'}:raise ValueError('plan_must_end_with_standby_or_return')
            represented={ident for step in normalized if step['action'] in {'pickup','deliver','wait'} for ident in step['order_ids']}
            unfinished={ident for ident,o in self.s['orders'].items() if o['status'] not in {'delivered','failed'}}
            if unfinished-represented:raise ValueError('plan_missing_unfinished_orders')
            loaded={ident for ident,o in self.s['orders'].items() if o['loaded']}
            delivered={ident for ident,o in self.s['orders'].items() if o['status']=='delivered'}
            for step in normalized:
                if step['action'] not in {'pickup','deliver'}:continue
                for ident in step['order_ids']:
                    order=self.s['orders'][ident]
                    if order['status']=='delivered':continue
                    if order['status']=='failed':raise ValueError('plan_service_for_closed_order:'+ident)
                    if step['action']=='pickup':
                        if ident in delivered:raise ValueError('plan_pickup_after_delivery:'+ident)
                        loaded.add(ident)
                        if len(loaded)>self.s['capacity']:raise ValueError('plan_exceeds_actual_capacity_deliver_before_next_pickup')
                    elif ident not in delivered:
                        if ident not in loaded:raise ValueError('plan_delivery_before_pickup:'+ident+':'+self.stations[order['pickup_station']]['label'])
                        loaded.remove(ident);delivered.add(ident)
            plan={'steps':normalized,'reason':reason,'planning_version':self.s['planning_version'],'wall_time':time.time(),'sim_time':self.s['sim_time'],'operation_id':a['operation_id']}
            self.s['delivery_plan']=plan;self.event('delivery_plan_published',plan=plan);return {'ok':True,'plan':plan,'planning_version':self.s['planning_version']}
        return super().mutate(name,a)
    def plan_view(self):
        plan=copy.deepcopy(self.s.get('delivery_plan'))
        if not plan:return None
        plan['stale']=plan['planning_version']!=self.s.get('planning_version',0);current=False
        for step in plan['steps']:
            orders=[self.s['orders'][i] for i in step['order_ids']];action=step['action'];at=self.at(step['station_id']) and self.stopped()
            arrival_proof=any(n['goal_station']==step['station_id'] and n['status']=='succeeded' and n.get('completed_wall',0)>=plan['wall_time'] for n in self.s['navigation_tasks'].values())
            arrival_proof=arrival_proof or bool(step.get('arrival_at_publish'))
            done=(all(o['loaded'] or o['status']=='delivered' for o in orders) if action=='pickup' else all(o.get('receipt') for o in orders) if action=='deliver' else self.s['receivers'][step['station_id']] if action=='wait' else self.finished() if action=='standby' else at or arrival_proof)
            failed=any(o['status']=='failed' for o in orders)
            active=not current and not done and not failed
            if active:current=True
            step['status']='completed' if done else 'failed' if failed else 'running' if active and (at or self.nav and self.nav['status'] in ACTIVE and self.nav['goal_station']==step['station_id']) else 'planned'
        return plan
    def finished(self):
        orders=list(self.s['orders'].values())
        if not orders or any(o['status'] not in {'delivered','failed'} or o['loaded'] for o in orders) or not self.stopped():return False
        plan=self.s.get('delivery_plan');target=plan['steps'][-1]['station_id'] if plan else next((o['dropoff_station'] for o in reversed(orders) if o.get('receipt')),None)
        return bool(target and self.at(target))
    def ready_actions(self,snapshot=None):
        snapshot=self.s if snapshot is None else snapshot
        groups={};space=max(0,snapshot['capacity']-sum(o['loaded'] for o in snapshot['orders'].values()))
        for ident,order in snapshot['orders'].items():
            if order['status'] in {'delivered','failed'}:continue
            action='deliver' if order['loaded'] else 'pickup'
            if action=='pickup' and not space:continue
            station=order['dropoff_station'] if action=='deliver' else order['pickup_station']
            key=(action,station)
            group=groups.setdefault(key,{'action':action,'station_id':station,'station_label':self.stations[station]['label'],'order_ids':[],'orders':[],'receiver_available':snapshot['receivers'][station]})
            group['order_ids'].append(ident)
            group['orders'].append({'order_id':ident,'priority':order['priority'],'deadline_remaining_sim_seconds':order['deadline_sim']-snapshot['sim_time']})
            if action=='pickup':group['max_packages']=min(space,len(group['order_ids']))
        return list(groups.values())
    def require_ready_goal(self,station):
        if self.s.get('mhs_outcome')=='incomplete':raise ValueError('new_run_required_after_incomplete')
        if any(o['status'] not in {'delivered','failed'} for o in self.s['orders'].values()) and station not in {a['station_id'] for a in self.ready_actions()}:
            raise ValueError('goal_has_no_ready_pickup_or_loaded_delivery_read_ready_actions')
    def state(self,since_event_id=0):
        state=super().state(since_event_id)
        state['cargo_count']=sum(o['loaded'] for o in state['orders'].values())
        state['available_capacity']=max(0,state['capacity']-state['cargo_count'])
        state['ready_actions']=self.ready_actions(state)
        state['at_station_labels']=[self.stations[ident]['label'] for ident in state['at_stations']]
        for order in state['orders'].values():
            order.update(pickup_label=self.stations[order['pickup_station']]['label'],dropoff_label=self.stations[order['dropoff_station']]['label'],deadline_remaining_sim_seconds=order['deadline_sim']-state['sim_time'])
        if hasattr(self,'routes'):
            state.update(crowd=self.routes.crowd(),hospital=self.routes.cfg,map_version=self.routes.version)
            qualification=self.root/'configs/navigation-qualification-v2.json'
            if qualification.exists():
                evidence=json.loads(qualification.read_text())
                if evidence.get('station_geometry_sha256')==station_digest(self.stations):state['station_navigation_evidence']=evidence
            state['speed_limit_mps']=self.routes.speed_limit([self.pose['x'],self.pose['y']],state['crowd']) if self.pose else None
            state['navigation_health']=self.backend.health() if hasattr(self.backend,'health') else None
            feedback=getattr(self.backend,'latest_route_feedback',None)
            state['navigation_progress']=dict(feedback,fresh=bool(self.nav and feedback['nav_task_id']==self.nav['nav_task_id'] and time.time()-feedback['wall_time']<5)) if feedback else None
            state['motion_clear']=bool(state['pose_fresh'] and state['crowd']['fresh'] and self.pose and not people_gate(self.pose,state['crowd']['people'])[1])
        return state
    def set_scene_event(self,kind,value):
        with self.lock:
            if kind=='closed_zone':
                ident=value['zone_id']
                if ident not in [z['id'] for z in self.routes.cfg['zones']]:raise ValueError('unknown_zone')
                closed=set(self.s['closed_zones']);closed.add(ident) if value['closed'] else closed.discard(ident);self.s['closed_zones']=sorted(closed)
                if self.nav and self.nav['status'] in ACTIVE:self.request_stop('route_environment_changed')
            elif kind=='crowd_mode':
                if value['mode'] not in ['normal','crowded','crossing']:raise ValueError('invalid_crowd_mode')
                p=self.root/'runs/crowd-command-v2.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps({'id':uuid.uuid4().hex,**value}));tmp.replace(p)
            else:raise ValueError('unknown_scene_event')
            self.s['planning_version']+=1;self.event('scene_event',event_type=kind,value=value);self.save();return {'ok':True}
    def verify(self,run_id=None):
        report=super().verify(run_id)
        metric_keys=['total','delivered','on_time','failed','unfinished','receipts','error_deliveries']
        report['run_totals']={key:copy.deepcopy(report[key]) for key in metric_keys}
        orders=[self.s['orders'][ident] for ident in self.s.get('segment_order_ids',list(self.s['orders']))]
        delivered=[o for o in orders if o['status']=='delivered']
        report.update(segment=self.s.get('segment',0),order_ids=[o['order_id'] for o in orders],metrics_scope='current_delivery_segment',
                      total=len(orders),delivered=len(delivered),on_time=sum(o['receipt']['sim_time']<=o['deadline_sim'] for o in delivered),
                      failed=[o['order_id'] for o in orders if o['status']=='failed'],unfinished=[o['order_id'] for o in orders if o['status'] not in {'delivered','failed'}],
                      receipts=[o['receipt'] for o in delivered],error_deliveries=sum(r['package_id']!=o['package_id'] or r['station']!=o['dropoff_station'] or math.hypot(r['arrival_pose']['x']-self.stations[r['station']]['x'],r['arrival_pose']['y']-self.stations[r['station']]['y'])>0.5 for o in delivered for r in [o['receipt']]))
        report.update(protocol='hospital-v2',mhs_outcome=self.s.get('mhs_outcome'),complete_task=self.finished(),final_station=next((k for k in self.stations if self.at(k)),None),final_stopped=self.stopped(),remaining_cargo=sum(o['loaded'] for o in self.s['orders'].values()),route_safety=getattr(self.backend,'safety_metrics',None),route_safety_scope='adapter_process_since_start_not_delivery_segment',simulated_crowd=True)
        return report
