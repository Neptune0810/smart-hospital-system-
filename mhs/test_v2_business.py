import json,shutil,tempfile,time,unittest
from pathlib import Path
from .engine_v2 import HospitalV2
from .motion_limits import limited_twist
from .proximity import ContinuousBlock,people_gate
from .outcome import delivery_wait_flags

ROOT=Path(__file__).resolve().parents[1]
class Backend:
    def ready(self):return True
    def zero(self):pass
    def cancel(self):pass
    def validate_route(self,route):pass
    def start_route(self,tid,route):self.started=(tid,route['route_id'])

class V2BusinessTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.cache').mkdir(exist_ok=True)
        self.directory=tempfile.TemporaryDirectory(dir=ROOT/'.cache');self.root=Path(self.directory.name)
        for folder in ['configs','maps','runs']:(self.root/folder).mkdir()
        for relative in ['configs/hospital-v2.json','maps/carter_hospital_navigation.png','maps/carter_hospital_navigation.yaml']:shutil.copy2(ROOT/relative,self.root/relative)
        self.stations=json.loads((ROOT/'configs/stations.json').read_text())
        self.engine=HospitalV2(self.root,self.stations);self.engine.backend=Backend();self.place('pharmacy')
    def tearDown(self):
        self.engine.db.close()
        if self.root.resolve().parent!=(ROOT/'.cache').resolve():raise RuntimeError('temporary_test_directory_outside_workspace_cache')
        self.directory.cleanup()
    def place(self,station):
        p=self.stations[station]
        (self.root/'runs/physical-pose.json').write_text(json.dumps({'position':[p['x'],p['y'],0],'wall_time':time.time()}))
        (self.root/'runs/crowd-v2.json').write_text(json.dumps({'people':[],'wall_time':time.time()}))
        start=self.engine.s['sim_time']+10
        self.engine.telemetry(p['x'],p['y'],0,0,start);self.engine.telemetry(p['x'],p['y'],0,0,start+2)
    def order(self):return self.engine.create_delivery_orders([{'pickup_station':'药房','destination':'A站'}],'create')['orders'][0]
    def test_point_to_point_handoff_and_standby(self):
        o=self.order();self.assertEqual(o['pickup_station'],'pharmacy');self.assertEqual(o['deadline_sim'],912)
        self.engine.do('pickup_package',{'order_id':o['order_id'],'operation_id':'load'})
        with self.assertRaisesRegex(ValueError,'not_at_destination'):self.engine.do('confirm_delivery',{'order_id':o['order_id'],'package_id':o['package_id'],'operation_id':'wrong-place'})
        self.place('station_a');self.engine.do('confirm_delivery',{'order_id':o['order_id'],'package_id':o['package_id'],'operation_id':'handoff'})
        self.assertTrue(self.engine.finished());self.assertTrue(self.engine.verify()['complete_task']);self.assertFalse(self.engine.verify()['returned_to_depot'])
    def test_idempotency_and_stale_plan(self):
        o=self.order();self.assertEqual(self.order()['order_id'],o['order_id'])
        a={'operation_id':'plan-old','planning_version':0,'reason':'交接后待命','steps':[{'action':'deliver','station_id':'station_a','order_ids':[o['order_id']]},{'action':'standby','station_id':'station_a'}]}
        with self.assertRaisesRegex(ValueError,'plan_state_changed'):self.engine.do('publish_delivery_plan',a)
        a['steps'].insert(0,{'action':'pickup','station_id':'pharmacy','order_ids':[o['order_id']]})
        a.update(operation_id='plan-new',planning_version=1);self.engine.do('publish_delivery_plan',a)
        self.assertFalse(self.engine.plan_view()['stale'])
    def test_capacity_and_pickup_location(self):
        orders=self.engine.create_delivery_orders([{'pickup_station':'药房','destination':'A站'}]*3,'three')['orders']
        for index,o in enumerate(orders[:2]):self.engine.do('pickup_package',{'order_id':o['order_id'],'operation_id':'load'+str(index)})
        with self.assertRaisesRegex(ValueError,'capacity_exceeded'):self.engine.do('pickup_package',{'order_id':orders[2]['order_id'],'operation_id':'over'})
    def test_unknown_same_point_and_wrong_package(self):
        with self.assertRaisesRegex(ValueError,'unknown_place'):self.engine.create_delivery_orders([{'destination':'火星'}],'unknown')
        with self.assertRaisesRegex(ValueError,'must_differ'):self.engine.create_delivery_orders([{'pickup_station':'药房','destination':'药房'}],'same')
        o=self.order();self.engine.do('pickup_package',{'order_id':o['order_id'],'operation_id':'load'})
        with self.assertRaisesRegex(ValueError,'package_mismatch'):self.engine.do('confirm_delivery',{'order_id':o['order_id'],'package_id':'wrong','operation_id':'mismatch'})

    def test_quantity_generates_distinct_packages_and_retries_same_ids(self):
        items=[{'pickup_station':'药房','destination':'A站','quantity':2,'package_label':'药品'}]
        result=self.engine.create_delivery_orders(items,'two')
        self.assertEqual(len(result['orders']),2)
        self.assertEqual(len({o['package_id'] for o in result['orders']}),2)
        self.assertEqual(self.engine.create_delivery_orders(items,'two'),result)
        self.assertEqual(self.engine.verify()['total'],2)
        with self.assertRaisesRegex(ValueError,'invalid_package_quantity'):
            self.engine.create_delivery_orders([{'destination':'A站','quantity':True}],'invalid-bool')
        self.assertEqual(self.engine.verify()['total'],2)
    def test_rejected_creation_is_idempotent_and_cannot_change_arguments(self):
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'unknown_place'):
                self.engine.create_delivery_orders([{'destination':'火星','quantity':1}],'rejected-create')
        with self.assertRaisesRegex(ValueError,'operation_id_conflict'):
            self.engine.create_delivery_orders([{'destination':'A站','quantity':1}],'rejected-create')
        self.assertEqual(self.engine.verify()['total'],0)
    def test_route_validation_and_environment_change(self):
        result=self.engine.route_options('station_a');self.assertTrue(result['routes']);route=result['routes'][0]
        self.assertEqual(route['validation'],'nav2_verified') # fake backend, unit verification only
        self.engine.guard.lease(True)
        task=self.engine.do('navigate_route',{'route_id':route['route_id'],'operation_id':'nav'})
        self.assertEqual(task['route_id'],route['route_id'])
        with self.assertRaisesRegex(ValueError,'navigation_busy'):self.engine.do('navigate_route',{'route_id':route['route_id'],'operation_id':'nav2'})
        called=[];self.engine.backend.prepare_route_query=lambda:called.append(True)
        with self.assertRaisesRegex(ValueError,'requires_stopped_navigation'):self.engine.route_options('station_b')
        self.assertEqual(called,[])
        self.engine.set_scene_event('closed_zone',{'zone_id':'waiting_corridor','closed':True})
        self.assertFalse(self.engine.guard.armed)

    def test_new_orders_require_current_public_plan_before_motion(self):
        first=self.order();self.engine.do('pickup_package',{'order_id':first['order_id'],'operation_id':'load'})
        state=self.engine.state();self.assertEqual(state['cargo_count'],1);self.assertEqual(state['available_capacity'],1)
        route=self.engine.route_options('A站')['routes'][0]
        self.engine.guard.lease(True)
        with self.assertRaisesRegex(ValueError,'publish_current_delivery_plan'):
            self.engine.do('navigate_route',{'route_id':route['route_id'],'operation_id':'missing-plan'})
        self.assertFalse(self.engine.guard.armed)
        plan={'planning_version':state['planning_version'],'operation_id':'plan','reason':'先送普通件，末站待命。','steps':[{'action':'navigate','station_id':'station_a','order_ids':[first['order_id']]},{'action':'deliver','station_id':'station_a','order_ids':[first['order_id']]},{'action':'standby','station_id':'station_a'}]}
        self.engine.do('publish_delivery_plan',plan)
        urgent=self.engine.create_delivery_orders([{'pickup_station':'药房','destination':'C站','priority':'urgent','quantity':1}],'urgent')['orders'][0]
        with self.assertRaisesRegex(ValueError,'publish_current_delivery_plan'):
            self.engine.do('navigate_route',{'route_id':route['route_id'],'operation_id':'stale-plan'})
        self.assertFalse(self.engine.guard.armed)
        plan.update(operation_id='new-plan',planning_version=self.engine.s['planning_version'],reason='新增急件后按当前容量调整安排。')
        with self.assertRaisesRegex(ValueError,'plan_missing_unfinished_orders'):
            self.engine.do('publish_delivery_plan',plan)
        plan.update(operation_id='complete-plan')
        plan['steps'].insert(-1,{'action':'pickup','station_id':'pharmacy','order_ids':[urgent['order_id']]})
        plan['steps'].insert(-1,{'action':'deliver','station_id':'station_c','order_ids':[urgent['order_id']]})
        self.engine.do('publish_delivery_plan',plan)
        task=self.engine.do('navigate_route',{'route_id':route['route_id'],'operation_id':'fresh-plan'})
        self.assertEqual(task['goal_station'],'station_a')
    def test_stale_crowd_and_operator_stop(self):
        (self.root/'runs/crowd-v2.json').write_text(json.dumps({'people':[],'wall_time':0}))
        with self.assertRaisesRegex(ValueError,'not_ready'):self.engine.route_options('A站')
        self.engine.stop_run('stop',self.engine.s['run_id'])
        with self.assertRaisesRegex(ValueError,'run_stopped'):self.order()

    def test_unpicked_destination_and_cached_route_are_refused_before_motion(self):
        route=self.engine.route_options('A站')['routes'][0]
        order=self.order()
        actions=self.engine.state()['ready_actions']
        self.assertEqual([(a['action'],a['station_id']) for a in actions],[('pickup','pharmacy')])
        with self.assertRaisesRegex(ValueError,'goal_has_no_ready'):
            self.engine.route_options('A站')
        self.engine.do('publish_delivery_plan',{'planning_version':1,'operation_id':'with-pickup','reason':'先药房取货，再A站交接','steps':[{'action':'pickup','station_id':'pharmacy','order_ids':[order['order_id']]},{'action':'deliver','station_id':'station_a','order_ids':[order['order_id']]},{'action':'standby','station_id':'station_a'}]})
        self.engine.guard.lease(True)
        with self.assertRaisesRegex(ValueError,'goal_has_no_ready'):
            self.engine.do('navigate_route',{'route_id':route['route_id'],'operation_id':'cached-before-pickup'})
        self.assertFalse(self.engine.guard.armed);self.assertFalse(hasattr(self.engine.backend,'started'))
        self.engine.do('pickup_package',{'order_id':order['order_id'],'operation_id':'now-load'})
        self.assertEqual([(a['action'],a['station_id']) for a in self.engine.state()['ready_actions']],[('deliver','station_a')])

    def test_public_plan_checks_pickup_dependency_and_capacity(self):
        orders=self.engine.create_delivery_orders([{'pickup_station':'药房','destination':'A站','quantity':3}],'three-plan')['orders']
        ids=[o['order_id'] for o in orders]
        a={'planning_version':1,'operation_id':'missing-pickup','reason':'装货后送A','steps':[{'action':'deliver','station_id':'station_a','order_ids':ids},{'action':'standby','station_id':'station_a'}]}
        with self.assertRaisesRegex(ValueError,'plan_delivery_before_pickup'):self.engine.do('publish_delivery_plan',a)
        a.update(operation_id='overload');a['steps'].insert(0,{'action':'pickup','station_id':'pharmacy','order_ids':ids})
        with self.assertRaisesRegex(ValueError,'plan_exceeds_actual_capacity'):self.engine.do('publish_delivery_plan',a)
        a.update(operation_id='valid-batches');a['steps']=[{'action':'pickup','station_id':'pharmacy','order_ids':ids[:2]},{'action':'deliver','station_id':'station_a','order_ids':ids[:2]},{'action':'pickup','station_id':'pharmacy','order_ids':ids[2:]},{'action':'deliver','station_id':'station_a','order_ids':ids[2:]},{'action':'standby','station_id':'station_a'}]
        self.engine.do('publish_delivery_plan',a);self.assertFalse(self.engine.plan_view()['stale'])

    def test_permanent_unavailability_closes_incomplete_without_discarding_cargo(self):
        order=self.order();self.engine.do('pickup_package',{'order_id':order['order_id'],'operation_id':'load'})
        self.place('station_a');self.engine.receiver('station_a',False)
        self.assertFalse(delivery_wait_flags(self.engine.s)['delivery_wait_station_a'])
        self.engine.s['sim_time']+=151;self.engine.tick()
        self.assertEqual(self.engine.s['mhs_outcome'],'incomplete')
        report=self.engine.verify();self.assertFalse(report['complete_task'])
        self.assertEqual(report['remaining_cargo'],1);self.assertEqual(report['delivered'],0)
        self.assertFalse(self.engine.s['receivers']['station_a'])
        self.assertTrue(delivery_wait_flags(self.engine.s)['delivery_wait_station_a'])
        budget=dict(self.engine.s['mhs_budget']);self.engine.s['sim_time']+=4000;self.engine.tick()
        self.assertEqual(self.engine.s['mhs_budget'],budget)
        self.assertEqual(self.order()['order_id'],order['order_id'])
        with self.assertRaisesRegex(ValueError,'new_run_required_after_incomplete'):
            self.engine.create_delivery_orders([{'pickup_station':'药房','destination':'A站'}],'new-after-incomplete')
        with self.assertRaisesRegex(ValueError,'new_run_required_after_incomplete'):self.engine.do('navigate_route',{'route_id':'unused','operation_id':'closed-motion'})
        self.engine.reset();self.assertIsNone(self.engine.s.get('mhs_outcome'))

    def test_initial_station_step_keeps_real_completion_after_departure(self):
        order=self.order()
        self.engine.do('publish_delivery_plan',{'operation_id':'initial-at-pickup','planning_version':1,'reason':'先取药，再送A站待命','steps':[
            {'action':'navigate','station_id':'pharmacy','order_ids':[order['order_id']]},
            {'action':'pickup','station_id':'pharmacy','order_ids':[order['order_id']]},
            {'action':'deliver','station_id':'station_a','order_ids':[order['order_id']]},
            {'action':'navigate','station_id':'pharmacy','order_ids':[]},
            {'action':'standby','station_id':'station_a','order_ids':[]}]})
        self.engine.do('pickup_package',{'order_id':order['order_id'],'operation_id':'load'})
        self.place('station_a')
        steps=self.engine.plan_view()['steps']
        self.assertEqual(steps[0]['status'],'completed')
        self.assertEqual(steps[0]['arrival_at_publish']['source'],'validated_station_pose')
        self.assertNotIn('arrival_at_publish',steps[3])
        self.assertNotEqual(steps[3]['status'],'completed')

    def test_stop_freezes_receiver_timeout_and_report(self):
        order=self.order()
        self.engine.receiver('station_a',False)
        self.engine.stop_run('stop-before-timeout',self.engine.s['run_id'])
        budget=dict(self.engine.s['mhs_budget'])
        before=json.loads(json.dumps(order))
        self.engine.s['sim_time']+=200
        self.engine.tick()
        self.assertEqual(self.engine.s['orders'][order['order_id']],before)
        self.assertEqual(self.engine.s['mhs_budget'],budget)
        self.assertNotIn('order_failed',[event['kind'] for event in self.engine.state()['events']])

    def test_invalid_next_order_preserves_completed_segment(self):
        o=self.order();self.engine.do('pickup_package',{'order_id':o['order_id'],'operation_id':'load'})
        self.place('station_a');self.engine.do('confirm_delivery',{'order_id':o['order_id'],'package_id':o['package_id'],'operation_id':'handoff'})
        self.engine.tick();budget=dict(self.engine.s['mhs_budget'])
        with self.assertRaisesRegex(ValueError,'invalid_priority'):
            self.engine.create_delivery_orders([{'destination':'B站','priority':'invalid'}],'next-invalid')
        self.assertTrue(self.engine.s['mhs_finished']);self.assertEqual(self.engine.s['mhs_budget'],budget)
        self.engine.create_delivery_orders([{'pickup_station':'A站','destination':'B站'}],'next-valid')
        self.assertFalse(self.engine.s['mhs_finished']);self.assertEqual(self.engine.s['segment'],1)
        report=self.engine.verify()
        self.assertEqual(report['total'],1);self.assertEqual(report['delivered'],0)
        self.assertEqual(report['receipts'],[]);self.assertEqual(report['run_totals']['total'],2)
        self.assertEqual(report['run_totals']['delivered'],1)

    def test_already_at_station_and_reset_keep_v2_contract(self):
        result=self.engine.route_options('药房')
        self.assertTrue(result['already_at_destination']);self.assertEqual(result['routes'],[])
        self.engine.reset()
        self.assertEqual(self.engine.s['closed_zones'],[])
        self.assertEqual(self.engine.s['protocol'],'hospital-v2')
        self.assertEqual(self.engine.s['segment'],0)

    def test_nonzero_route_with_endpoints_in_same_grid_cell(self):
        routes=self.engine.routes;center=routes.point(routes.snap([-30,10]))
        pose={'x':center[0]-.24,'y':center[1]-.24}
        station={'id':'nearby','x':center[0]+.24,'y':center[1]+.24}
        self.assertEqual(routes.snap([pose['x'],pose['y']]),routes.snap([station['x'],station['y']]))
        row=routes.options(pose,station)[0]
        self.assertEqual(row['points'][0],[pose['x'],pose['y']]);self.assertEqual(row['points'][-1],[station['x'],station['y']])
        self.assertGreater(row['length_m'],.5);self.assertGreater(row['estimated_navigation_seconds'],1)

    def test_slowing_preserves_turning_curvature(self):
        v,w=limited_twist(.8,.7,.2)
        self.assertLessEqual(v,.2);self.assertLessEqual(abs(w),.45)
        self.assertAlmostEqual(w/v,.7/.8)
        self.assertEqual(limited_twist(0.,.8,.2),(0.,.45))

    def test_block_timer_resets_during_velocity_silence(self):
        timer=ContinuousBlock();self.assertEqual(timer.observe(True,100),'started')
        self.assertEqual(timer.observe(False,108),'finished')
        timer.observe(True,114);self.assertEqual(timer.duration(114),0)
        self.assertEqual(timer.duration(124),10)
        self.assertFalse(people_gate({'x':0,'y':0,'yaw':0},[{'position':[3,0]}])[1])
        self.assertTrue(people_gate({'x':0,'y':0,'yaw':0},[{'position':[1,0]}])[1])

    def test_scene_clock_rebase_restores_trajectory_recording(self):
        self.engine.reset();p=self.stations['pharmacy']
        self.engine.telemetry(p['x'],p['y'],0,0,0)
        self.engine.telemetry(p['x']+.1,p['y'],0,0,2)
        self.assertEqual(self.engine.s['start_sim'],0)
        self.assertEqual([v[0] for v in self.engine.s['trajectory']],[0,2])
        self.assertTrue(self.engine.stopped())
        self.assertEqual(self.engine.resolve('station-a'),'station_a')

if __name__=='__main__':unittest.main()
