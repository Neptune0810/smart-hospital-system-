import unittest
from . import test_business
from .engine import MHSDelivery

class ArrangementTests(unittest.TestCase):
    setUp=test_business.BusinessTests.setUp
    tearDown=test_business.BusinessTests.tearDown
    def plan(self,order,op='plan'):
        return self.e.do('publish_delivery_plan',dict(steps=[
            dict(action='pickup',station_id='depot',order_ids=[order['order_id']]),
            dict(action='deliver',station_id='station_a',order_ids=[order['order_id']]),
            dict(action='return',station_id='depot',order_ids=[])],reason='先送 A，然后返库。',planning_version=self.e.s['planning_version'],operation_id=op))
    def test_plan_is_metadata_and_real_receipts_drive_progress(self):
        o=self.e.create_delivery_orders([{'destination':'station_a'}],'create')['orders'][0]
        r=self.plan(o);self.assertEqual(self.plan(o),r);self.assertIsNone(self.e.nav)
        self.assertEqual([s['status'] for s in self.e.plan_view()['steps']],['planned']*3)
        self.e.do('pickup_package',dict(order_id=o['order_id'],operation_id='pick'))
        self.assertEqual(self.e.plan_view()['steps'][0]['status'],'completed')
        self.assertEqual(self.e.plan_view()['steps'][1]['status'],'planned')
        self.e.telemetry(1,0,0,0,20);self.e.telemetry(1,0,0,0,22)
        self.e.do('confirm_delivery',dict(order_id=o['order_id'],package_id=o['package_id'],operation_id='handoff'))
        self.assertEqual(self.e.plan_view()['steps'][1]['status'],'completed')
        self.assertEqual(self.e.plan_view()['steps'][2]['status'],'planned')
        self.e.telemetry(0,0,0,0,25);self.e.telemetry(0,0,0,0,26)
        self.assertEqual(self.e.plan_view()['steps'][2]['status'],'completed')
    def test_new_order_invalidates_plan_and_old_version_is_rejected(self):
        o=self.e.create_delivery_orders([{'destination':'station_a'}],'c')['orders'][0];self.plan(o)
        old=self.e.s['planning_version'];self.e.create_delivery_orders([{'destination':'station_a','priority':'urgent'}],'urgent')
        self.assertTrue(self.e.plan_view()['stale'])
        with self.assertRaisesRegex(ValueError,'plan_state_changed'):
            self.e.do('publish_delivery_plan',dict(steps=[dict(action='return',station_id='depot',order_ids=[])],reason='返库',planning_version=old,operation_id='stale'))
    def test_owner_and_operator_stop_survive_restart(self):
        run=self.e.s['run_id'];self.e.claim_session('session1',run)
        with self.assertRaisesRegex(ValueError,'another_session'):self.e.claim_session('session2',run)
        self.e.stop_run('stop',run)
        with self.assertRaisesRegex(ValueError,'run_stopped_by_operator'):
            self.e.create_delivery_orders([{'destination':'station_a'}],'after-stop')
        with self.assertRaisesRegex(ValueError,'run_stopped_by_operator'):self.e.decision('late',run,'session1')
        self.e.db.close();self.e=MHSDelivery(self.root,{'depot':{'x':0,'y':0},'station_a':{'x':1,'y':0}},False)
        self.assertTrue(self.e.s['operator_stopped']);self.assertEqual(self.e.s['controller_session'],'session1')
        with self.assertRaisesRegex(ValueError,'run_stopped_by_operator'):
            self.e.do('navigate_to',dict(station_id='depot',operation_id='after-restart'))
        self.e.reset();self.assertFalse(self.e.s.get('operator_stopped'));self.assertFalse(self.e.s.get('controller_session'))
    def test_invalid_plan_has_no_effect(self):
        with self.assertRaisesRegex(ValueError,'unknown_plan_order'):
            self.e.do('publish_delivery_plan',dict(steps=[dict(action='deliver',station_id='station_a',order_ids=['missing'])],reason='送 A',planning_version=0,operation_id='invalid'))
        self.assertNotIn('delivery_plan',self.e.s)
    def test_refill_does_not_highlight_two_returns_or_mark_final_return_done(self):
        o=self.e.create_delivery_orders([{'destination':'station_a'}],'create')['orders'][0]
        self.e.do('publish_delivery_plan',dict(steps=[
            dict(action='return',station_id='depot',order_ids=[]),
            dict(action='pickup',station_id='depot',order_ids=[o['order_id']]),
            dict(action='deliver',station_id='station_a',order_ids=[o['order_id']]),
            dict(action='return',station_id='depot',order_ids=[])],reason='回库取货后配送，再返库。',planning_version=self.e.s['planning_version'],operation_id='refill-plan'))
        self.e.do('pickup_package',dict(order_id=o['order_id'],operation_id='pick'))
        self.e.telemetry(.5,0,0,.3,15)
        self.assertEqual([s['status'] for s in self.e.plan_view()['steps']],['completed','completed','planned','planned'])

if __name__=='__main__':unittest.main()
