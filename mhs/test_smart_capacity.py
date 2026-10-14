"""Local logic tests only; none is real navigation, an ACK or Agent evidence."""
import argparse
import asyncio
import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from . import smart_boundaries, smart_capacity as capacity
from .smart_faults import Faults


def complete_immediate(coroutine):
    """Exercise mocked async boundaries without Windows socketpair/event-loop IO."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('local_unit_fixture_unexpectedly_requires_async_io')


def fixture():
    specimens = {'c1': 'S1', 'c2': 'S2', 'c3': 'S3'}
    orders = {'c1': 'O1', 'c2': 'O2', 'c3': 'O3'}
    locations = {'S1': {'location': 'robot'}, 'S2': {'location': 'robot'}, 'S3': {'location': capacity.CABINET}}
    cabinet = {'specimens': ['S3'], 'fresh': True, 'available': True}
    transfers = {'T1': {'status': 'committed', 'operation_id': 'p-pickup-c1', 'specimen_id': 'S1', 'location': 'robot'},
                 'T2': {'status': 'committed', 'operation_id': 'p-pickup-c2', 'specimen_id': 'S2', 'location': 'robot'}}
    ward = {'epoch': 'E', 'scene_fresh': True, 'scene_sample': {'run_id': 'R', 'epoch': 'E', 'seq': 10, 'wall_time': 100.},
            'specimens': copy.deepcopy(locations), 'devices': {capacity.CABINET: copy.deepcopy(cabinet)},
            'operations': {'p-pickup-c1': {'status': 'completed', 'transfer_id': 'T1'},
                           'p-pickup-c2': {'status': 'completed', 'transfer_id': 'T2'}},
            'transfers': transfers, 'watch': {'enabled': False}, 'incidents': []}
    rows = {oid: {'specimen_id': specimens[label], 'loaded': label != 'c3', 'status': 'onboard' if label != 'c3' else 'created'}
            for label, oid in orders.items()}
    state = {'run_id': 'R', 'controller_session': 'Q', 'capacity': 2, 'cargo_count': 2,
             'orders': rows, 'smart_ward': ward, 'at_stations': [capacity.STATION],
             'operator_stopped': True, 'motion_guard': {'armed': False},
             'navigation_tasks': {'N': {'goal_station': capacity.STATION, 'route_id': 'route-C', 'status': 'succeeded'}},
             'planning_version': 3}
    observed_transfers = {tid: {'operation_id': t['operation_id'], 'order_id': orders[label], 'specimen_id': specimens[label], 'location': 'robot'}
                          for label, (tid, t) in zip(('c1', 'c2'), transfers.items())}
    observation = {'run_id': 'R', 'epoch': 'E', 'seq': 11, 'wall_time': 101.,
                   'specimens': copy.deepcopy(locations), 'devices': {capacity.CABINET: copy.deepcopy(cabinet)},
                   'transfers': observed_transfers}
    ack = {'run_id': 'R', 'epoch': 'E', 'operation_id': 'p-pickup-c1', 'status': 'completed', 'wall_time': 99.,
           'result': {'transfer_id': 'T1', 'order_id': 'O1', 'specimen_id': 'S1', 'cabinet_id': capacity.CABINET,
                      'direction': 'pickup', 'location': 'robot'}}
    return state, observation, ack, specimens, orders


class CapacityEvidenceTests(unittest.TestCase):
    def test_only_exact_production_capacity_error_is_accepted(self):
        self.assertTrue(capacity.exact_capacity_error({'status': 'failed', 'error': {'message': 'capacity_exceeded'}}))
        for reason in ('robot_capacity_exceeded', 'source_cabinet_full', 'receiver_cabinet_full',
                       'handoff_requires_fresh_scene_and_real_stationary_arrival', 'Hub_job_timeout'):
            with self.subTest(reason=reason):
                self.assertFalse(capacity.exact_capacity_error({'error': reason}))

    def test_pickup_requires_original_ack_business_commit_and_new_scene(self):
        state, observation, ack, _, _ = fixture()
        with patch.object(capacity, 'physical_stationary', return_value=True):
            self.assertTrue(capacity.pickup_proved(state, observation, ack, 'p-pickup-c1', 'O1', 'S1', 'R', 'E', 9))
            changes = ('ack_identity', 'ack_location', 'old_sample', 'business_only', 'not_committed', 'not_onboard')
            for change in changes:
                s, o, a = copy.deepcopy((state, observation, ack))
                if change == 'ack_identity': a['epoch'] = 'OLD'
                if change == 'ack_location': a['result']['location'] = capacity.CABINET
                if change == 'old_sample': s['smart_ward']['scene_sample']['seq'] = 9
                if change == 'business_only': o['specimens']['S1']['location'] = capacity.CABINET
                if change == 'not_committed': s['smart_ward']['transfers']['T1']['status'] = 'prepared'
                if change == 'not_onboard': s['orders']['O1']['loaded'] = False
                with self.subTest(change=change):
                    self.assertFalse(capacity.pickup_proved(s, o, a, 'p-pickup-c1', 'O1', 'S1', 'R', 'E', 9))
        with patch.object(capacity, 'physical_stationary', return_value=False):
            self.assertFalse(capacity.pickup_proved(state, observation, ack, 'p-pickup-c1', 'O1', 'S1', 'R', 'E', 9))

    def test_full_cargo_requires_all_three_unique_scene_and_business_owners(self):
        state, observation, _, specimens, orders = fixture()
        self.assertTrue(capacity.ownership_proved(state, observation, specimens, orders))
        for owner in ('business', 'scene', 'cabinet', 'loaded'):
            s, o = copy.deepcopy((state, observation))
            if owner == 'business': s['smart_ward']['specimens']['S3']['location'] = 'robot'
            if owner == 'scene': o['specimens']['S1']['location'] = capacity.CABINET
            if owner == 'cabinet': o['devices'][capacity.CABINET]['specimens'] = []
            if owner == 'loaded': s['orders']['O3']['loaded'] = True
            with self.subTest(owner=owner):
                self.assertFalse(capacity.ownership_proved(s, o, specimens, orders))

    def test_refusal_projection_detects_ack_navigation_and_transfer_mutation(self):
        state, observation, _, _, _ = fixture()
        baseline = capacity.refusal_projection(state, observation, {'acks/a.json': 'old'})
        s = copy.deepcopy(state)
        s['navigation_tasks']['N']['status'] = 'running'
        self.assertNotEqual(baseline, capacity.refusal_projection(s, observation, {'acks/a.json': 'old'}))
        o = copy.deepcopy(observation)
        o['transfers']['T3'] = {'specimen_id': 'S3'}
        self.assertNotEqual(baseline, capacity.refusal_projection(state, o, {'acks/a.json': 'old'}))
        self.assertNotEqual(baseline, capacity.refusal_projection(state, observation, {'acks/a.json': 'new'}))


class CapacityDriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.cache')
        self.root = Path(self.temp.name)
        for name in capacity.SOURCES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# local unit fixture only\n', encoding='utf-8')
        self.root_patch = patch.object(smart_boundaries, 'ROOT', self.root)
        self.env_patch = patch.dict(os.environ, {'HOSPITAL_ROOT': str(self.root)})
        async def immediate_thread(function, *args, **kwargs):
            return function(*args, **kwargs)
        self.thread_patch = patch.object(capacity.asyncio, 'to_thread', new=immediate_thread)
        self.root_patch.start()
        self.env_patch.start()
        self.thread_patch.start()
        self.driver = self.make_driver()
        self.driver.run_id, self.driver.epoch, self.driver.session = 'R', 'E', 'Q'
        self.driver.nav_expected = True

    def tearDown(self):
        self.thread_patch.stop()
        self.env_patch.stop()
        self.root_patch.stop()
        self.temp.cleanup()

    def make_driver(self, wall=1800):
        return capacity.Capacity(argparse.Namespace(wall_seconds=wall, output=str(self.root / 'runs/smart-ward-v1/acceptance' / uuid.uuid4().hex)))

    def configure_fixture(self):
        state, observation, _, specimens, orders = fixture()
        self.driver.specimens, self.driver.orders = specimens, orders
        self.driver.prefix = 'p'
        self.driver.incidents = {'c1': 'I1', 'c2': 'I2', 'c3': 'I3'}
        return state, observation

    def test_fixed_finite_scope_and_original_cleanup_reserve(self):
        self.assertEqual(self.driver.deadline - self.driver.work_deadline, 60)
        self.assertEqual(self.driver.report['limits']['maximum_orders'], 3)
        self.assertEqual(self.driver.report['limits']['maximum_navigation_tasks'], 1)
        self.assertEqual(self.driver.report['limits']['fault_commands'], 20)
        self.assertFalse(self.driver.report['full_plan_acceptance_passed'])
        for wall in (179, 1801, True, float('nan')):
            with self.subTest(wall=wall), self.assertRaisesRegex(ValueError, '180_to_1800'):
                self.make_driver(wall)
        with patch('sys.stderr'):
            with self.assertRaises(SystemExit): capacity.arguments(['--wall-seconds', '1801'])

    def test_independent_state_accepts_three_but_refuses_extra_order_or_navigation(self):
        state, _ = self.configure_fixture()
        self.driver.guard = Mock()
        self.driver.api = Mock(return_value=state)
        self.assertEqual(len(self.driver.state()['orders']), 3)
        for change in ('fourth_order', 'fourth_specimen', 'third_transfer', 'second_nav', 'wrong_nav_goal'):
            value = copy.deepcopy(state)
            if change == 'fourth_order': value['orders']['O4'] = {}
            if change == 'fourth_specimen': value['smart_ward']['specimens']['S4'] = {}
            if change == 'third_transfer': value['smart_ward']['transfers']['T3'] = {}
            if change == 'second_nav': value['navigation_tasks']['N2'] = {'goal_station': capacity.STATION}
            if change == 'wrong_nav_goal': value['navigation_tasks']['N']['goal_station'] = 'station_b'
            self.driver.api.return_value = value
            with self.subTest(change=change), self.assertRaises(RuntimeError): self.driver.state()

    def test_model_and_workbench_guard_remains_fail_closed(self):
        self.driver.remaining = Mock(return_value=1)
        self.driver.audit_zero_model = Mock(side_effect=RuntimeError('unexpected_model_request'))
        with patch('mhs.smart_faults.workbench_absent', return_value={'absent': True}):
            with self.assertRaisesRegex(RuntimeError, 'unexpected_model_request'): self.driver.guard()
        self.driver.audit_zero_model = Mock()
        with patch('mhs.smart_faults.workbench_absent', return_value={'absent': False}):
            with self.assertRaisesRegex(RuntimeError, 'workbench_must_remain_absent'): self.driver.guard()

    def test_exact_mutation_caps_and_same_id_retry_only(self):
        self.configure_fixture()
        self.driver.expected_route_id = 'route-C'
        async def exercise():
            with patch.object(Faults, 'rpc', new=AsyncMock(return_value={'accepted': True})) as parent:
                await self.driver.rpc('hub/call', {'device': 'hospital-robot-01', 'tool': 'navigate_route', 'arguments': {'route_id': 'route-C'}})
                with self.assertRaisesRegex(RuntimeError, 'navigation_call_scope'):
                    await self.driver.rpc('hub/call', {'device': 'hospital-robot-01', 'tool': 'navigate_route', 'arguments': {'route_id': 'route-C'}})
                for label in ('c1', 'c2', 'c3', 'c3'):
                    await self.driver.rpc('hub/call', {'device': capacity.CABINET, 'tool': 'handoff_order',
                        'arguments': {'order_id': self.driver.orders[label], 'operation_id': 'p-pickup-' + label}})
                with self.assertRaisesRegex(RuntimeError, 'handoff_call_scope'):
                    await self.driver.rpc('hub/call', {'device': capacity.CABINET, 'tool': 'handoff_order',
                        'arguments': {'order_id': 'O3', 'operation_id': 'p-pickup-c3-new'}})
                self.assertEqual(parent.await_count, 5)
        complete_immediate(exercise())
        self.assertEqual((self.driver.navigation_calls, self.driver.handoff_calls), (1, 4))
        with patch.object(Faults, 'scene_command', return_value=({}, {}, {})):
            for label in ('c1', 'c2', 'c3'): self.driver.scene_command(capacity.CABINET, label)
            with self.assertRaisesRegex(RuntimeError, 'insert_command_scope'): self.driver.scene_command(capacity.CABINET, 'c4')

    def test_unknown_handoff_response_consumes_allowance_without_retry(self):
        self.configure_fixture()
        message = {'device': capacity.CABINET, 'tool': 'handoff_order', 'arguments': {'order_id': 'O1', 'operation_id': 'p-pickup-c1'}}
        async def exercise():
            with patch.object(Faults, 'rpc', new=AsyncMock(side_effect=TimeoutError('transport_unknown'))):
                with self.assertRaises(TimeoutError): await self.driver.rpc('hub/call', message)
                with self.assertRaisesRegex(RuntimeError, 'handoff_call_scope'): await self.driver.rpc('hub/call', message)
        complete_immediate(exercise())
        self.assertEqual(self.driver.handoff_calls, 1)

    def test_third_plan_unloads_before_next_pickup_and_uses_latest_version(self):
        state, _ = self.configure_fixture()
        self.driver.astate = AsyncMock(return_value=state)
        self.driver.tool = Mock(return_value={'ok': True})
        complete_immediate(self.driver.publish_plan(third=True))
        name, args = self.driver.tool.call_args.args
        self.assertEqual(name, 'publish_service_plan')
        self.assertEqual(args['planning_version'], 3)
        actions = [(s['action'], s.get('incident_id')) for s in args['steps']]
        self.assertLess(actions.index(('deliver', 'I1')), actions.index(('pickup', 'I3')))
        self.assertLess(actions.index(('deliver', 'I2')), actions.index(('pickup', 'I3')))

    def test_late_navigation_state_cannot_pass(self):
        state, observation = self.configure_fixture()
        clock = [0.]
        self.driver.work_deadline = 1740.
        self.driver.hub = type('Hub', (), {'jobs': {'J': {'status': 'done'}}})()
        self.driver.publish_plan = AsyncMock(return_value={})
        self.driver.aapi = AsyncMock(return_value={'routes': [{'route_id': 'route-C', 'validation': 'nav2_verified', 'distance_m': 1}]})
        self.driver.device_state = AsyncMock()
        self.driver.rpc = AsyncMock(return_value={'accepted': True, 'job': 'J'})
        async def late_state():
            clock[0] = capacity.ACK_NAVIGATION_LEG_WALL_SECONDS + .1
            return state
        self.driver.astate = AsyncMock(side_effect=late_state)
        self.driver.observation = Mock(return_value=observation)
        with patch.object(capacity.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(capacity, 'arrived', return_value=True) as actual:
            with self.assertRaisesRegex(TimeoutError, 'real_c_arrival_not_proved'):
                complete_immediate(self.driver.arrival_c())
        actual.assert_not_called()

    def test_refusal_requires_capacity_reason_and_no_new_ack(self):
        state, observation = self.configure_fixture()
        self.driver.astate = AsyncMock(return_value=state)
        self.driver.observation = Mock(return_value=observation)
        self.driver.remaining = Mock(return_value=100)
        self.driver.rpc = AsyncMock(return_value={'accepted': True, 'job': 'J'})
        self.driver.finish = AsyncMock(return_value={'status': 'failed', 'error': 'capacity_exceeded'})
        with patch.object(capacity, 'physical_stationary', return_value=True), patch.object(capacity, 'scene_files', return_value={'acks/first': 'hash'}):
            proof = complete_immediate(self.driver.refusal())
            self.assertEqual(proof['expected_reason'], 'capacity_exceeded')
            self.driver.finish.return_value = {'status': 'failed', 'error': 'handoff_requires_fresh_scene_and_real_stationary_arrival'}
            with self.assertRaisesRegex(RuntimeError, 'capacity_refusal_not_proved'): complete_immediate(self.driver.refusal())
            self.driver.finish.return_value = {'status': 'done', 'error': 'capacity_exceeded'}
            with self.assertRaisesRegex(RuntimeError, 'capacity_refusal_not_proved'): complete_immediate(self.driver.refusal())
        self.driver.finish.return_value = {'status': 'failed', 'error': 'capacity_exceeded'}
        with patch.object(capacity, 'physical_stationary', return_value=True), patch.object(capacity, 'scene_files', side_effect=[{}, {'acks/third.json': 'new'}]):
            with self.assertRaisesRegex(AssertionError, 'changed_ownership_navigation_command_or_ack'): complete_immediate(self.driver.refusal())

    def cleanup_fixture(self):
        state, observation = self.configure_fixture()
        self.driver.start_attempted = True
        self.driver.qualification = {'qualification_id': 'F', 'session_key': 'Q', 'run_id': 'R', 'epoch': 'E'}
        self.driver.results[capacity.CASES[4]]['status'] = 'passed'
        self.driver.aapi = AsyncMock(side_effect=[state, {'qualification': dict(self.driver.qualification, ended_wall=90.)}])
        self.driver.astate = AsyncMock(return_value=state)
        path = self.root / 'runs/smart-ward-v1/scene/fault-state.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(self.driver.qualification, active=False, holds={}, paused=False)))
        return state, observation

    def test_cleanup_proves_three_new_samples_and_retains_full_cargo(self):
        state, observation = self.cleanup_fixture()
        self.driver.observation = Mock(side_effect=[dict(observation, seq=n) for n in (12, 13, 14)])
        with patch.object(capacity, 'physical_stationary', return_value=True), patch.object(capacity.asyncio, 'sleep', new=AsyncMock()):
            result = complete_immediate(self.driver.cleanup())
        self.assertTrue(result['physical_stop_confirmed'])
        self.assertEqual(result['new_scene_sequences'], [12, 13, 14])
        self.assertTrue(self.driver.report['final_cargo_retained'])
        self.assertEqual(result['state']['cargo_count'], 2)
        self.assertEqual(result['state']['smart_ward']['specimens']['S3']['location'], capacity.CABINET)
        self.assertEqual(self.driver.results[capacity.CASES[5]]['status'], 'passed')
        self.assertEqual([c.args[0] for c in self.driver.aapi.call_args_list if c.args], ['/api/admin/smart-ward/fault-qualification/end'])

    def test_cleanup_rejects_lost_cargo_and_does_not_invent_delivery(self):
        state, observation = self.cleanup_fixture()
        observation['specimens']['S1']['location'] = capacity.CABINET
        self.driver.observation = Mock(side_effect=[dict(observation, seq=n) for n in (12, 13, 14)])
        with patch.object(capacity, 'physical_stationary', return_value=True), patch.object(capacity.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaisesRegex(AssertionError, 'preserve_two_cargo'): complete_immediate(self.driver.cleanup())
        self.assertNotIn('final_cargo_retained', self.driver.report)

    def test_cleanup_cannot_reuse_one_static_sample(self):
        _, observation = self.cleanup_fixture()
        self.driver.deadline = 30.
        clock = [0.]
        self.driver.observation = Mock(return_value=observation)
        async def advance(_): clock[0] += 1
        with patch.object(capacity, 'physical_stationary', return_value=True), patch.object(capacity.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(capacity.asyncio, 'sleep', new=AsyncMock(side_effect=advance)):
            with self.assertRaisesRegex(TimeoutError, 'actual_stop_not_confirmed'): complete_immediate(self.driver.cleanup())
        self.assertNotIn('final_cargo_retained', self.driver.report)

    def configure_mock_run(self):
        self.driver.begin = AsyncMock()
        self.driver.cleanup = AsyncMock(return_value={'physical_stop_confirmed': True})
        self.driver.audit_zero_model = Mock(return_value={'request_sent_count': 0, 'inference_intent_count': 0})
        async def completed_cases():
            for name in capacity.CASES: self.driver.results[name]['status'] = 'passed'
            self.driver.insert_commands = self.driver.order_requests = 3
            self.driver.handoff_calls, self.driver.navigation_calls = 4, 1
            self.driver.report['final_cargo_retained'] = True
        self.driver.qualify = AsyncMock(side_effect=completed_cases)

    def test_closed_report_keeps_unexecuted_na_and_is_never_agent_success(self):
        self.configure_mock_run()
        with patch('builtins.print'):
            code = complete_immediate(self.driver.run_async())
        self.assertEqual(code, 2)
        report = json.loads((self.driver.directory / 'report.json').read_text())
        self.assertTrue(report['requested_case_passed'])
        self.assertEqual(report['outcome'], 'partial_NA')
        self.assertFalse(report['full_plan_acceptance_passed'])
        self.assertEqual(report['cases']['receiver_full']['status'], 'NA')
        self.assertEqual(report['cases']['real_agent_acceptance']['status'], 'NA')

    def test_failed_runtime_is_stopped_once_preserved_and_never_retried(self):
        self.configure_mock_run()
        self.driver.qualify = AsyncMock(side_effect=RuntimeError('original_handoff_unknown'))
        with patch('builtins.print'):
            code = complete_immediate(self.driver.run_async())
        self.assertEqual(code, 1)
        self.driver.qualify.assert_awaited_once()
        self.driver.cleanup.assert_awaited_once()
        self.assertIn('original_handoff_unknown', self.driver.report['error'])
        self.assertFalse(self.driver.report['requested_case_passed'])

    def test_source_change_cannot_turn_mock_passed_cases_into_qualification_pass(self):
        self.configure_mock_run()
        (self.root / 'mhs/smart_scene.py').write_text('# changed during qualification\n')
        with patch('builtins.print'):
            code = complete_immediate(self.driver.run_async())
        self.assertEqual(code, 1)
        self.assertFalse(self.driver.report['source_unchanged'])
        self.assertIn('source_changed', self.driver.report['cleanup_error'])
        self.assertFalse(self.driver.report['requested_case_passed'])


if __name__ == '__main__':
    unittest.main()
