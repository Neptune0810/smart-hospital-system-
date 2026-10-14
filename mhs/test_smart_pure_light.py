"""Business-only synthetic feedback. Never Isaac, navigation or model evidence."""
import ast
import copy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from .engine_v2 import HospitalV2
from . import test_smart_ward as fixtures


class PureLightTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SmartWardTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.engine=self.fixture.engine
        self.fixture.start()

    def plan_light(self, incident, op='pure-plan'):
        return self.engine.publish_service_plan(self.engine.s['planning_version'],[
            {'action':'set_brightness','incident_id':incident['incident_id'],'device_id':'hospital-light-301'},
            {'action':'verify','incident_id':incident['incident_id'],'device_id':'hospital-sensor-301'},
            {'action':'standby'}], '原地恢复照明并读取新反馈',op)

    def recover_light(self, name='restore-light'):
        op=self.engine.device_action('hospital-light-301','set_brightness',{'brightness_percent':70,'operation_id':name})
        self.fixture.lamp_ack(op)
        self.fixture.devices['hospital-light-301']['brightness_percent']=70
        self.fixture.devices['hospital-sensor-301']['relative_illumination']=76
        return op

    def transport(self, sid='first', final='lab'):
        order=self.fixture.sample(sid=sid)
        iid=order['incident_id']
        self.engine.publish_service_plan(self.engine.s['planning_version'],[
            {'action':'pickup','incident_id':iid}, {'action':'deliver','incident_id':iid},
            {'action':'standby','station_id':final}], '送检后在指定末站停稳','transport-'+sid)
        pickup=self.engine.device_action('hospital-cabinet-b','handoff_order',
            {'order_id':order['order_id'],'operation_id':'pickup-'+sid})
        self.fixture.handoff_ack(pickup)
        self.fixture.place('lab');self.fixture.observe()
        receive=self.engine.device_action('hospital-cabinet-lab','handoff_order',
            {'order_id':order['order_id'],'operation_id':'receive-'+sid})
        self.fixture.handoff_ack(receive)
        return order

    def test_pure_light_standby_destination_is_rejected_without_publishing_or_navigation(self):
        incident=self.fixture.low_light()
        previous=copy.deepcopy(self.engine.s['smart_ward'].get('service_plan'))
        with self.assertRaisesRegex(ValueError,'lighting_only_standby_must_stay_in_place'):
            self.engine.publish_service_plan(self.engine.s['planning_version'],[
                {'action':'set_brightness','incident_id':incident['incident_id']},
                {'action':'standby','station_id':'depot'}], '错误地返库','bad-depot-plan')
        self.assertEqual(self.engine.s['smart_ward'].get('service_plan'),previous)
        self.assertFalse(self.engine.s['navigation_tasks'])
        self.assertIsNone(self.engine._final_station())

    def test_every_robot_transport_step_is_rejected_even_if_attached_to_a_light_incident(self):
        incident=self.fixture.low_light()
        for action in ('create_delivery','pickup','navigate','deliver'):
            with self.assertRaisesRegex(ValueError,'lighting_only_plan_cannot_include_robot_transport'):
                self.engine.publish_service_plan(self.engine.s['planning_version'],[
                    {'action':action,'incident_id':incident['incident_id'],'station_id':'depot'},
                    {'action':'standby'}], '不应移动','bad-'+action)
        self.assertFalse(self.engine.s['navigation_tasks'])

    def test_legacy_bad_light_plan_cannot_authorize_motion_or_create_a_nav_task(self):
        self.fixture.low_light()
        self.engine.s['smart_ward']['service_plan']={'steps':[{'action':'standby','station_id':'depot'}]}
        before=copy.deepcopy(self.engine.s['navigation_tasks'])
        with patch.object(HospitalV2,'mutate') as parent, patch.object(self.engine.backend,'start_route') as backend:
            with self.assertRaisesRegex(ValueError,'lighting_only_requires_stationary_sensor_verification'):
                self.engine.mutate('navigate_route',{'route_id':'unused','operation_id':'must-not-dispatch'})
        parent.assert_not_called();backend.assert_not_called()
        self.assertEqual(self.engine.s['navigation_tasks'],before)
        self.assertIsNone(self.engine._final_station())

    def test_three_fresh_restoration_reads_verify_and_seal_in_place_without_any_nav(self):
        incident=self.fixture.low_light();self.plan_light(incident)
        position=copy.deepcopy(self.engine.pose)
        self.recover_light()
        self.assertFalse(self.engine.verify_services()['all_resolved'])
        for _ in range(2):self.fixture.observe()
        self.assertFalse(self.engine.verify_services()['ready_to_finalize'])
        self.fixture.observe()
        report=self.engine.verify_services()
        self.assertTrue(report['all_resolved']);self.assertTrue(report['ready_to_finalize'])
        self.fixture.consume_notifications()
        receipt=self.engine.finalize_episode(self.engine.s['run_id'],'session-current')
        self.assertEqual(receipt['episode'],1)
        self.assertEqual(self.engine.pose,position)
        self.assertFalse(self.engine.s['navigation_tasks'])
        self.assertFalse(self.engine.s['orders'])
        self.assertEqual(report['order_ids'],[])

    def test_delivered_transport_history_and_old_standby_do_not_authorize_next_light_motion(self):
        order=self.transport(final='lab')
        self.fixture.consume_notifications()
        self.engine.verify_services();first=self.engine.finalize_episode(self.engine.s['run_id'],'session-current')
        original=self.engine.data_dir/first['report_ref']['path'];before=original.read_bytes()
        # Synthetic unit relocation isolates the historic-plan/current-pose edge.
        self.fixture.place('station_b');self.fixture.observe()
        incident=self.fixture.low_light()
        self.assertEqual(self.engine.s['smart_ward']['episode'],2)
        self.assertEqual(self.engine.s['orders'][order['order_id']]['status'],'delivered')
        self.assertFalse(self.engine.s['orders'][order['order_id']]['loaded'])
        self.assertFalse(self.engine._episode_has_transport())
        self.assertIsNone(self.engine._final_station())
        with self.assertRaisesRegex(ValueError,'lighting_only_requires_stationary_sensor_verification'):
            self.engine.mutate('navigate_route',{'route_id':'old-final','operation_id':'no-old-final'})
        self.plan_light(incident,'episode2-light-plan');self.recover_light('episode2-light')
        for _ in range(3):self.fixture.observe()
        report=self.engine.verify_services();self.assertTrue(report['ready_to_finalize'])
        self.assertEqual((report['total'],report['order_ids'],report['receipts']),(1,[],[]))
        self.fixture.consume_notifications()
        second=self.engine.finalize_episode(self.engine.s['run_id'],'session-current')
        self.assertEqual(second['episode'],2);self.assertEqual(original.read_bytes(),before)
        self.assertFalse(self.engine.s['navigation_tasks'])

    def test_current_resolved_transport_still_requires_declared_final_and_only_that_route(self):
        order=self.transport(final='station_a')
        self.assertEqual(self.engine.s['smart_ward']['incidents'][order['incident_id']]['status'],'resolved')
        self.assertTrue(self.engine._episode_has_transport())
        self.assertEqual(self.engine._final_station(),'station_a')
        self.assertFalse(self.engine.finished());self.assertFalse(self.engine.verify_services()['ready_to_finalize'])
        with patch.object(self.engine.routes,'selected',return_value={'destination':'station_c'}):
            with self.assertRaisesRegex(ValueError,'only_published_final_station_is_allowed'):
                self.engine.mutate('navigate_route',{'route_id':'wrong','operation_id':'wrong-final'})
        with patch.object(self.engine.routes,'selected',return_value={'destination':'station_a'}), patch.object(HospitalV2,'mutate',return_value={'synthetic_delegate':True}) as parent:
            result=self.engine.mutate('navigate_route',{'route_id':'final-a','operation_id':'allowed-final'})
        self.assertTrue(result['synthetic_delegate']);parent.assert_called_once()
        self.fixture.place('station_a');self.fixture.observe()
        self.assertTrue(self.engine.finished());self.assertTrue(self.engine.verify_services()['ready_to_finalize'])

    def test_any_actual_loaded_cargo_keeps_transport_classification_and_pending_handoff_lock(self):
        order=self.fixture.sample()
        operation=self.engine.device_action('hospital-cabinet-b','handoff_order',
            {'order_id':order['order_id'],'operation_id':'protected-pickup'})
        with self.assertRaisesRegex(ValueError,'pending_handoff'):
            self.engine.mutate('navigate_route',{'route_id':'unused','operation_id':'before-ack'})
        self.fixture.handoff_ack(operation)
        self.assertTrue(self.engine._episode_has_transport())
        self.assertTrue(self.engine.s['orders'][order['order_id']]['loaded'])

    def test_global_nonterminal_transport_cannot_be_silently_sealed_by_a_new_light_episode(self):
        order=self.transport(final='lab');self.engine.verify_services()
        self.fixture.consume_notifications()
        self.engine.finalize_episode(self.engine.s['run_id'],'session-current')
        incident=self.fixture.low_light()
        self.plan_light(incident)
        # Deliberately inconsistent synthetic data tests fail-closed completion,
        # not a production API that can manufacture a historical pending order.
        self.engine.s['orders'][order['order_id']]['status']='queued'
        with self.assertRaisesRegex(ValueError,'service_plan_missing_transport_steps'):
            self.plan_light(incident,'reject-inconsistent-plan')
        self.recover_light()
        for _ in range(3):self.fixture.observe()
        self.assertFalse(self.engine._final_station_ready());self.assertFalse(self.engine.finished())
        self.assertFalse(self.engine.verify_services()['ready_to_finalize'])
        with self.assertRaisesRegex(ValueError,'episode_not_ready_to_finalize|current_episode_verification_required'):
            self.engine.finalize_episode(self.engine.s['run_id'],'session-current')


if __name__=='__main__':unittest.main(verbosity=2)
