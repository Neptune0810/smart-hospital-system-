import tempfile,json,time,unittest
from pathlib import Path
from .engine import MHSDelivery
from .guard import MotionGuard

class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'runs').mkdir();(self.root/'runs/state.sqlite').write_bytes(b'original-frozen')
        self.e=MHSDelivery(self.root,{'depot':{'x':0,'y':0},'station_a':{'x':1,'y':0}},False)
        self.e.telemetry(0,0,0,0,10);self.e.telemetry(0,0,0,0,12)
    def tearDown(self):self.e.db.close();self.temp.cleanup()
    def test_relative_deadline_retry_and_isolation(self):
        a=self.e.create_delivery_orders([{'destination':'A护士站'}],'order1')
        self.e.telemetry(0,0,0,0,15)
        b=self.e.create_delivery_orders([{'destination':'A护士站'}],'order1')
        self.assertEqual(a,b);self.assertEqual(a['orders'][0]['deadline_sim'],252)
        self.assertEqual((self.root/'runs/state.sqlite').read_bytes(),b'original-frozen')
        self.assertEqual(len(self.e.s['orders']),1)
        with self.assertRaisesRegex(ValueError,'operation_id_conflict'):
            self.e.create_delivery_orders([{'destination':'station_a','priority':'urgent'}],'order1')
    def test_receiver_timer_starts_at_outage(self):
        self.e.create_delivery_orders([{'destination':'station_a'}],'o')
        self.e.telemetry(0,0,0,0,200);self.e.receiver('station_a',False);self.e.tick()
        self.assertEqual(next(iter(self.e.s['orders'].values()))['status'],'pending')
        self.e.telemetry(0,0,0,0,349);self.e.tick()
        self.assertEqual(next(iter(self.e.s['orders'].values()))['status'],'pending')
        self.e.telemetry(0,0,0,0,350);self.e.tick()
        self.assertEqual(next(iter(self.e.s['orders'].values()))['status'],'failed')
    def test_completed_budget_remains_frozen_after_idle_drift(self):
        order=self.e.create_delivery_orders([{'destination':'station_a'}],'frozen-order')['orders'][0]
        self.e.do('close_order',dict(order_id=order['order_id'],reason='unit_expected_undelivered',operation_id='close'))
        self.e.tick();self.assertTrue(self.e.s['mhs_finished'])
        budget=self.e.s['mhs_budget']
        decisions=budget['decisions']
        self.e.telemetry(5,5,0,0,1000)
        self.e.tick();self.e.decision('late-notification')
        self.assertIsNone(budget['stopped_reason'])
        self.assertEqual(budget['decisions'],decisions)
        self.assertEqual(budget['end_sim'],12)
    def test_rejected_creation_cannot_be_reused_for_different_orders(self):
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'invalid_station'):
                self.e.create_delivery_orders([{'destination':'unknown'}],'bad-create')
        with self.assertRaisesRegex(ValueError,'operation_id_conflict'):
            self.e.create_delivery_orders([{'destination':'station_a'}],'bad-create')
        self.assertEqual(self.e.s['orders'],{})
    def test_active_delivery_cannot_navigate_without_corresponding_cargo(self):
        self.e.create_delivery_orders([{'destination':'station_a'}],'o')
        with self.assertRaisesRegex(ValueError,'pickup_required_before_delivery_navigation'):
            self.e.do('navigate_to',dict(station_id='station_a',operation_id='nav-empty'))
        self.assertIsNone(self.e.nav)
        self.assertFalse(self.e.guard.armed)
    def test_budget_refuses_new_work(self):
        self.e.create_delivery_orders([{'destination':'station_a'}],'o')
        for i in range(60):self.e.decision(str(i))
        self.e.tick()
        self.assertEqual(self.e.s['mhs_budget']['stopped_reason'],'decision_limit')
        with self.assertRaisesRegex(ValueError,'run_budget_exhausted'):
            self.e.create_delivery_orders([{'destination':'station_a'}],'new')
    def test_decision_rejects_wrong_run_without_changing_budget(self):
        self.e.create_delivery_orders([{'destination':'station_a'}],'o')
        with self.assertRaisesRegex(ValueError,'session_run_mismatch'):
            self.e.decision('wrong-run','other-run')
        self.assertEqual(self.e.s['mhs_budget']['decisions'],0)
        run_id=self.e.s['run_id']
        result=self.e.decision('correct-run',run_id)
        self.assertEqual(result['run_id'],run_id)
        self.assertEqual(result['budget']['decisions'],1)
        self.e.decision('correct-run',run_id)
        self.assertEqual(self.e.s['mhs_budget']['decisions'],1)
    def test_handoff_rejection_retry_does_not_count_twice(self):
        created=self.e.create_delivery_orders([{'destination':'station_a'}],'o')['orders'][0]
        self.e.do('pickup_package',dict(order_id=created['order_id'],operation_id='pickup'))
        self.e.telemetry(1,0,0,0,20);self.e.telemetry(1,0,0,0,22)
        self.e.receiver('station_a',False)
        a=dict(order_id=created['order_id'],package_id=created['package_id'],operation_id='handoff')
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'receiver_unavailable'):self.e.do('confirm_delivery',a)
        self.assertEqual(self.e.s['orders'][created['order_id']]['attempts'],1)
    def test_completed_run_needs_reset_and_metrics_start_at_order(self):
        self.e.s['path_length']=5
        order=self.e.create_delivery_orders([{'destination':'station_a'}],'o')['orders'][0]
        self.e.do('pickup_package',dict(order_id=order['order_id'],operation_id='pickup'))
        self.e.telemetry(1,0,0,0,20);self.e.telemetry(1,0,0,0,22)
        self.e.do('confirm_delivery',dict(order_id=order['order_id'],package_id=order['package_id'],operation_id='receipt'))
        self.e.telemetry(0,0,0,0,25);self.e.telemetry(0,0,0,0,26);self.e.tick()
        report=self.e.verify();self.assertEqual(report['elapsed_sim'],14)
        self.e.telemetry(0,0,0,0,30)
        self.assertEqual(self.e.verify()['elapsed_sim'],report['elapsed_sim'])
        self.assertEqual(self.e.verify()['wall_seconds'],report['wall_seconds'])
        with self.assertRaisesRegex(ValueError,'new_run_required_after_completion'):
            self.e.create_delivery_orders([{'destination':'station_a'}],'another')
    def test_navigation_retry_after_another_goal_keeps_original_task(self):
        class Backend:
            def __init__(self):self.tasks=[]
            def ready(self):return True
            def start(self,tid,station):self.tasks.append(tid)
            def zero(self):pass
            def cancel(self):pass
        self.e.backend=Backend();self.e.fresh=lambda:True;self.e.guard.lease(True)
        first=self.e.do('navigate_to',dict(station_id='station_a',operation_id='nav1'))
        self.e.nav_result(first['nav_task_id'],'succeeded')
        second=self.e.do('navigate_to',dict(station_id='depot',operation_id='nav2'))
        retry=self.e.do('navigate_to',dict(station_id='station_a',operation_id='nav1'))
        self.assertEqual(retry['nav_task_id'],first['nav_task_id'])
        self.assertEqual(self.e.s['navigation_tasks'][first['nav_task_id']]['status'],'succeeded')
        self.assertEqual(self.e.nav['nav_task_id'],second['nav_task_id'])
        self.assertEqual(len(self.e.backend.tasks),2)

class GuardTests(unittest.TestCase):
    def test_disconnect_and_no_automatic_resume(self):
        now=[0.];stops=[];g=MotionGuard(stops.append,lambda:now[0])
        g.lease(True);g.arm(True,True);now[0]=3.01
        self.assertFalse(g.tick());self.assertEqual(stops,['motion_lease_expired'])
        g.lease(True);self.assertFalse(g.tick())
        with self.assertRaisesRegex(ValueError,'stop_or_pose_not_confirmed'):g.arm(False,True)
        g.arm(True,True);self.assertTrue(g.tick())
    def test_stale_pose_stops_while_lease_alive(self):
        stops=[];g=MotionGuard(stops.append);g.lease(True);g.arm(True,True)
        self.assertFalse(g.tick(False));self.assertEqual(stops,['telemetry_stale'])
if __name__=='__main__':unittest.main()
