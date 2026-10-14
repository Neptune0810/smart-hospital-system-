"""Evidence classifier checks only; never starts a real demonstration."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .smart_acceptance import (Demonstration, REQUIRED_DEVICES, receiver_watch_records,
                               completion_evidence, summarize_ledger, manual_ui_evidence,
                               RECOVERY_ENVIRONMENT, recovery_restore_decision,
                               receiver_false_evidence, recovery_evidence_pass)
from .smart_ward import SmartWardV1
from .engine_v2 import HospitalV2


class RecoveryEnvironmentTests(unittest.TestCase):
    def fixture(self):
        pause = {'run_id': 'run01', 'epoch': 'epoch01', 'sample_seq': 10,
                 'wall_time': 100, 'sim_time': 10, 'operation_id': 'pause01'}
        internal = {'run_id': 'run01', 'sim_time': 20, 'pose': {'x': 0, 'y': 0},
                 'stations': {'lab': {'x': 0, 'y': 0}}, 'orders': {'O1': {'loaded': True}},
                 'smart_ward': {'epoch': 'epoch01', 'incidents': {},
                     'scene': {'run_id': 'run01', 'epoch': 'epoch01', 'seq': 11,
                               'wall_time': 103, 'sim_time': 20},
                     'devices': {'hospital-cabinet-lab': {'fresh': True, 'available': False,
                                 'seq': 11, 'wall_time': 103, 'sim_time': 20}}}}
        # Exercise the production public serializer, without constructing a
        # backend, database or simulator. HTTP state deliberately omits scene.
        engine = object.__new__(SmartWardV1)
        engine._ward_ready = True; engine._scene_fresh = lambda: True
        with patch.object(HospitalV2, 'state', return_value=internal):
            state = engine.state()
        self.assertNotIn('scene', state['smart_ward'])
        self.assertEqual(set(state['smart_ward']['scene_sample']),
                         {'run_id', 'epoch', 'seq', 'wall_time', 'sim_time'})
        return state, pause

    def driver(self, pause, waiting=False):
        driver = object.__new__(Demonstration)
        driver.manual_ui = False; driver.args = SimpleNamespace(case='recovery')
        driver.root = Path(__file__).resolve().parents[1]
        driver.run_id = 'run01'; driver.session = 'session01'; driver.seq_start = 0
        driver.details = {'receiver_paused': True, 'pause_sim': pause['sim_time'],
                          'pause_wall': pause['wall_time'], 'receiver_pause_boundary': pause}
        driver.watch_calls = unittest.mock.Mock(return_value=waiting)
        driver.inject = unittest.mock.Mock(return_value={'operation_id': 'environment01'})
        driver.record = unittest.mock.Mock()
        return driver

    def test_version_records_previous_environment_and_unchanged_business_limits(self):
        self.assertEqual(RECOVERY_ENVIRONMENT['version'], 'smart-recovery-environment-20261013.35')
        self.assertEqual(RECOVERY_ENVIRONMENT['prior_environment'],
                         {'restore_sim_upper_seconds': 60, 'restore_wall_upper_seconds': 45})
        self.assertEqual(RECOVERY_ENVIRONMENT['restore_wall_upper_seconds'], 300)
        self.assertEqual(RECOVERY_ENVIRONMENT['unchanged_business_limits'],
                         {'real_requests': 60, 'sim_seconds': 2400, 'wall_seconds': 3600,
                          'receiver_unavailable_sim_seconds': 150})

    def test_original_45_wall_point_no_longer_restores_without_watch(self):
        for elapsed_sim, elapsed_wall in ((10, 45), (59.999, 299.999)):
            decision = recovery_restore_decision(elapsed_sim, elapsed_wall, False, True)
            self.assertFalse(decision['restore']); self.assertFalse(decision['fallback'])

    def test_accepted_watch_restores_only_after_minimum_pause_and_fresh_false(self):
        self.assertFalse(recovery_restore_decision(9.999, 45, True, True)['restore'])
        self.assertFalse(recovery_restore_decision(10, 45, True, False)['restore'])
        decision = recovery_restore_decision(10, 45, True, True)
        self.assertTrue(decision['restore']); self.assertFalse(decision['fallback'])

    def test_each_upper_limit_without_watch_is_explicit_cleanup_timeout(self):
        for sim, wall, limits in ((60, 45, ['simulation_seconds']),
                                  (10, 300, ['wall_seconds']),
                                  (60, 300, ['simulation_seconds', 'wall_seconds'])):
            decision = recovery_restore_decision(sim, wall, False, True)
            self.assertTrue(decision['restore']); self.assertTrue(decision['fallback'])
            self.assertEqual(decision['reason'], 'receiver_wait_registration_timeout')
            self.assertEqual(decision['reached_limits'], limits)

    def test_unknown_false_evidence_at_limit_is_cleanup_and_cannot_pass(self):
        decision = recovery_restore_decision(60, 300, True, False)
        self.assertTrue(decision['restore']); self.assertTrue(decision['fallback'])
        self.assertEqual(decision['reason'], 'receiver_false_evidence_unknown')
        details = {'receiver_false_observed': True, 'receiver_false_evidence': {'passed': True},
                   'receiver_restored': True, 'agent_receiver_watch_observed': True}
        self.assertTrue(recovery_evidence_pass(details, True))
        details['receiver_environment_fallback'] = decision
        self.assertFalse(recovery_evidence_pass(details, True))
        details.pop('receiver_environment_fallback'); details.pop('receiver_false_evidence')
        self.assertFalse(recovery_evidence_pass(details, True))

    def test_invalid_elapsed_or_nonboolean_evidence_never_authorizes_restore(self):
        for invalid in (None, True, -1, '10', float('nan'), float('inf')):
            for sim, wall in ((invalid, 300), (60, invalid)):
                decision = recovery_restore_decision(sim, wall, True, True)
                self.assertFalse(decision['restore'])
                self.assertEqual(decision['reason'], 'receiver_wait_timing_unknown')
        for watch, fresh in ((1, True), (True, 1), (None, False)):
            self.assertFalse(recovery_restore_decision(60, 300, watch, fresh)['restore'])

    def test_fresh_post_pause_false_retains_actual_sample_reference(self):
        state, pause = self.fixture()
        proof = receiver_false_evidence(state, pause, 104)
        self.assertTrue(proof['passed'])
        self.assertEqual(proof['sample'], {'run_id': 'run01', 'epoch': 'epoch01',
            'device_id': 'hospital-cabinet-lab', 'seq': 11, 'wall_time': 103,
            'sim_time': 20, 'available': False, 'fresh': True, 'scene_seq': 11,
            'scene_wall_time': 103, 'pause_operation_id': 'pause01'})

    def test_stale_old_mismatched_or_missing_false_sample_cannot_be_evidence(self):
        changes = [lambda s, p: s['smart_ward'].update(scene_fresh=False),
                   lambda s, p: s['smart_ward']['devices']['hospital-cabinet-lab'].update(fresh=False),
                   lambda s, p: s['smart_ward']['devices']['hospital-cabinet-lab'].update(available=0),
                   lambda s, p: s['smart_ward']['devices']['hospital-cabinet-lab'].update(available=True),
                   lambda s, p: s['smart_ward']['devices']['hospital-cabinet-lab'].update(seq=10),
                   lambda s, p: s['smart_ward']['devices']['hospital-cabinet-lab'].update(wall_time=100),
                   lambda s, p: s['smart_ward']['devices']['hospital-cabinet-lab'].update(sim_time=9),
                   lambda s, p: s['smart_ward']['scene_sample'].update(seq=12),
                   lambda s, p: s['smart_ward']['scene_sample'].update(wall_time=102),
                   lambda s, p: s['smart_ward']['scene_sample'].update(sim_time=19),
                   lambda s, p: s['smart_ward']['scene_sample'].update(run_id='old-run'),
                   lambda s, p: s['smart_ward'].update(epoch='old-epoch'),
                   lambda s, p: s.update(run_id='old-run'),
                   lambda s, p: s['smart_ward'].pop('scene_sample'),
                   lambda s, p: s['smart_ward'].pop('devices'),
                   lambda s, p: p.pop('sample_seq')]
        for change in changes:
            with self.subTest(change=change):
                state, pause = self.fixture(); change(state, pause)
                self.assertFalse(receiver_false_evidence(state, pause, 104)['passed'])
        state, pause = self.fixture()
        for now in (102.999, 106.001):
            self.assertFalse(receiver_false_evidence(state, pause, now)['passed'])
        for malformed in (None, [], {'smart_ward': None}, {'smart_ward': {'devices': []}}):
            self.assertFalse(receiver_false_evidence(malformed, pause, 104)['passed'])

    def test_illegal_times_sequences_or_pause_metadata_cannot_be_fresh(self):
        for field in ('wall_time', 'sim_time', 'seq'):
            for invalid in (None, True, -1, '11', float('nan'), float('inf')):
                state, pause = self.fixture()
                state['smart_ward']['devices']['hospital-cabinet-lab'][field] = invalid
                self.assertFalse(receiver_false_evidence(state, pause, 104)['passed'])
        for invalid in (True, 11.0):
            state, pause = self.fixture(); state['smart_ward']['scene_sample']['seq'] = invalid
            self.assertFalse(receiver_false_evidence(state, pause, 104)['passed'])
        state, pause = self.fixture()
        for invalid_now in (True, None, float('nan'), float('inf')):
            self.assertFalse(receiver_false_evidence(state, pause, invalid_now)['passed'])
        for field in ('wall_time', 'sim_time', 'sample_seq', 'run_id', 'epoch'):
            state, pause = self.fixture(); pause.pop(field)
            self.assertFalse(receiver_false_evidence(state, pause, 104)['passed'])

    def test_driver_keeps_45_wall_pause_and_records_normal_watch_restore(self):
        state, pause = self.fixture(); state['sim_time'] = 30
        state['smart_ward']['scene_sample'].update(wall_time=144, sim_time=30)
        state['smart_ward']['devices']['hospital-cabinet-lab'].update(wall_time=144, sim_time=30)
        driver = self.driver(pause)
        with patch('mhs.smart_acceptance.time.time', return_value=145):
            driver.conditional_injections(state)
        driver.inject.assert_not_called()
        self.assertTrue(driver.details['receiver_false_evidence']['passed'])
        driver.watch_calls.return_value = True
        with patch('mhs.smart_acceptance.time.time', return_value=145):
            driver.conditional_injections(state)
        driver.inject.assert_called_once_with('set_receiver', {'available': True}, 'lab-restore')
        self.assertTrue(recovery_evidence_pass(driver.details, True))

    def test_timeout_is_latched_and_post_restore_watch_is_never_counted(self):
        state, pause = self.fixture()
        state['smart_ward']['scene_sample']['wall_time'] = 399
        state['smart_ward']['devices']['hospital-cabinet-lab']['wall_time'] = 399
        driver = self.driver(pause)
        with patch('mhs.smart_acceptance.time.time', return_value=400):
            driver.conditional_injections(state)
        self.assertTrue(driver.details['receiver_wait_registration_timeout'])
        self.assertEqual(driver.details['receiver_environment_fallback']['reached_limits'], ['wall_seconds'])
        self.assertFalse(recovery_evidence_pass(driver.details, True))
        driver.watch_calls.return_value = True
        with patch('mhs.smart_acceptance.time.time', return_value=401):
            driver.conditional_injections(state)
        self.assertEqual(driver.watch_calls.call_count, 1)
        self.assertFalse(driver.details['agent_receiver_watch_observed'])
        self.assertFalse(recovery_evidence_pass(driver.details, True))

    def test_pause_still_requires_loaded_and_within_three_metres(self):
        for loaded, distance, expected in ((False, 0, False), (True, 3.001, False), (True, 3, True)):
            state, pause = self.fixture(); state['orders']['O1']['loaded'] = loaded
            state['pose']['x'] = distance
            driver = self.driver(pause); driver.details = {}
            with patch('mhs.smart_acceptance.time.time', return_value=104), patch('mhs.smart_acceptance.ledger_rows', return_value=[]):
                driver.conditional_injections(state)
            self.assertEqual(driver.details.get('receiver_paused', False), expected)
            self.assertEqual(driver.inject.call_count, int(expected))
            if expected:
                driver.inject.assert_called_once_with('set_receiver', {'available': False}, 'lab-pause')
                self.assertEqual(driver.details['receiver_pause_boundary']['sample_seq'], 11)
                self.assertFalse(driver.details.get('receiver_false_observed', False), 'pre-pause sample cannot prove the new pause')

    def test_internal_scene_without_public_sample_is_not_fresh_evidence(self):
        state, pause = self.fixture()
        state['smart_ward']['scene'] = state['smart_ward'].pop('scene_sample')
        self.assertFalse(receiver_false_evidence(state, pause, 104)['passed'])

    def test_sample_must_be_after_pause_ack_wall_boundary_and_sequence(self):
        for boundary in ({'wall_time': 103}, {'wall_time': 104}, {'sample_seq': 11}):
            state, pause = self.fixture(); pause.update(boundary)
            self.assertFalse(receiver_false_evidence(state, pause, 104)['passed'])


class ReceiverWatchTests(unittest.TestCase):
    def rows(self):
        return [
            {'seq': 10, 'type': 'tool/call', 'data': {'toolUseId': 'wait1', 'name': 'watch_device',
                'args': {'device': 'hospital-robot-01', 'until': {'state': 'delivery_wait_lab', 'eq': True}}}},
            {'seq': 11, 'type': 'tool/result', 'data': {'toolUseId': 'wait1', 'isError': False,
                'content': [{'type': 'text', 'text': 'watch accepted'}]}},
        ]

    def test_exact_native_condition_retains_raw_call_and_result(self):
        rows = self.rows()
        self.assertEqual(receiver_watch_records(rows), [{'call': rows[0], 'result': rows[1]}])
        self.assertEqual(receiver_watch_records(rows, after_seq=10), [])

    def test_old_cabinet_target_wrong_condition_or_rejected_wait_cannot_pass(self):
        for changes in ({'device': 'hospital-cabinet-lab'},
                        {'until': {'state': 'available', 'eq': True}},
                        {'until': {'state': 'delivery_wait_lab', 'eq': False}},
                        {'until': {'state': 'delivery_wait_lab', 'eq': 1}}):
            rows = copy.deepcopy(self.rows())
            rows[0]['data']['args'].update(changes)
            self.assertEqual(receiver_watch_records(rows), [])
        rows = self.rows(); rows[1]['data']['isError'] = True
        self.assertEqual(receiver_watch_records(rows), [])
        self.assertEqual(receiver_watch_records(self.rows()[:1]), [])

    def test_input_and_arguments_ledger_shapes_are_supported(self):
        for key in ('input', 'arguments'):
            rows = self.rows(); rows[0]['data'][key] = rows[0]['data'].pop('args')
            self.assertEqual(len(receiver_watch_records(rows)), 1)


class AgentCompletionTests(unittest.TestCase):
    def fixture(self):
        state = {'run_id': 'run01', 'pose_fresh': True, 'stopped_confirmed': True, 'mhs_finished': True,
                 'at_stations': ['lab'], 'mhs_budget': {'end_wall': 120},
                 'smart_ward': {'episode': 1, 'verification_event_version': 8, 'scene_fresh': True,
                     'last_verification': {'episode': 1, 'event_version': 8},
                     'service_plan': {'steps': [{'action': 'standby', 'station_id': 'lab'}]}}}
        report = {'run_id': 'run01', 'episode': 1, 'verification_event_version': 8,
                  'ready_to_finalize': True, 'pending': [], 'incidents': []}
        rows = [{'seq': 1, 'type': 'turn/start', 'data': {}},
                {'seq': 10, 'type': 'tool/call', 'data': {'name': 'hospital_verify_services', 'toolUseId': 'v1'}},
                {'seq': 11, 'type': 'tool/result', 'data': {'toolUseId': 'v1', 'isError': False,
                   'content': [{'type': 'text', 'text': json.dumps({'ok': True, 'result': report})}]}},
                {'seq': 12, 'type': 'turn/end', 'data': {'reason': 'completed'}}]
        return state, rows

    def test_current_finalized_completed_turn_accepts_post_turn_effects(self):
        state, rows = self.fixture()
        rows.extend([{'seq': 13, 'type': 'cost/ledger', 'data': {'effectId': 'e1'}},
                     {'seq': 14, 'type': 'effect/settled', 'data': {'effectId': 'e1'}}])
        proof = completion_evidence(state, rows)
        self.assertTrue(proof['passed']); self.assertEqual(proof['terminal_turn']['seq'], 12)

    def test_stop_or_early_verify_cannot_manufacture_agent_completion(self):
        for change in [lambda state: state['mhs_budget'].pop('end_wall'),
                       lambda state: state.update(mhs_finished=False),
                       lambda state: state.update(operator_stopped={'reason': 'user_stop'}),
                       lambda state: state['mhs_budget'].update(stopped_reason='wall_time_limit'),
                       lambda state: state.update(at_stations=['station_b']),
                       lambda state: state['smart_ward']['last_verification'].update(event_version=7),
                       lambda state: state['smart_ward'].update(episode=2),
                       lambda state: state.update(pose_fresh=False)]:
            state, rows = self.fixture(); change(state)
            self.assertFalse(completion_evidence(state, rows)['passed'])
        state, rows = self.fixture()
        report = json.loads(rows[2]['data']['content'][0]['text'])
        report['result']['ready_to_finalize'] = False
        rows[2]['data']['content'][0]['text'] = json.dumps(report)
        self.assertFalse(completion_evidence(state, rows)['passed'])

    def test_error_terminal_or_later_turn_cannot_pass(self):
        state, rows = self.fixture(); rows[-1]['data']['reason'] = 'error'
        self.assertFalse(completion_evidence(state, rows)['passed'])
        state, rows = self.fixture(); rows.append({'seq': 13, 'type': 'turn/start', 'data': {}})
        self.assertFalse(completion_evidence(state, rows)['passed'])
        state, rows = self.fixture(); rows[2]['data']['isError'] = True
        self.assertEqual(summarize_ledger(rows)['verifications'], [])
        self.assertFalse(completion_evidence(state, rows)['passed'])


class ManualUITests(unittest.TestCase):
    def fixture(self):
        state = {'run_id': 'run01', 'controller_session': 'session01', 'orders': {}, 'pose': {'x': 0, 'y': 0},
                 'protocol': 'smart-ward-v1', 'navigation_ready': True, 'pose_fresh': True,
                 'physical_pose': {'x': 0, 'y': 0}, 'motion_guard': {'healthy': True}, 'stopped_confirmed': True,
                 'smart_ward': {'epoch': 'epoch01', 'watch': {'enabled': False}, 'scene_fresh': True,
                               'devices': {device: {} for device in REQUIRED_DEVICES},
                               'incidents': [], 'specimens': {}, 'operations': {}}}
        facade = {'profile': 'smart-ward-v1', 'operations': []}
        return state, facade

    def proof_fixture(self):
        state, facade = self.fixture(); ward = state['smart_ward']
        facade['operations'].append({'action': 'smart_start', 'status': 'completed', 'wall_time': 101,
                                     'new_session': 'session01', 'expected_run_id': 'run01'})
        for ident, kind, args, result in (
                ('insert01', 'insert_specimen', {'cabinet_id': 'hospital-cabinet-b', 'priority': 'normal'}, {'specimen_id': 'S1'}),
                ('dark01', 'set_brightness', {'room_id': 'room301', 'brightness_percent': 10},
                 {'room_id': 'room301', 'requested_percent': 10, 'actual_percent': 10})):
            facade['operations'].append({'action': 'smart_event', 'status': 'completed', 'wall_time': 102,
                'session_id': 'session01', 'expected_run_id': 'run01', 'operation_id': ident, 'kind': kind, 'arguments': args})
            identity = {'run_id': 'run01', 'epoch': 'epoch01', 'operation_id': ident}
            ward['operations'][ident] = {'status': 'completed', 'origin': 'operator',
                'command': {**identity, 'kind': kind, 'arguments': args},
                'ack': {**identity, 'status': 'completed', 'result': result}}
        ward['specimens']['S1'] = {'location': 'hospital-cabinet-lab'}
        ward['incidents'] = [{'kind': 'specimen_ready', 'specimen_id': 'S1', 'cabinet_id': 'hospital-cabinet-b'},
                             {'kind': 'low_light', 'room_id': 'room301'}]
        return state, facade

    def driver(self, directory, state, facade, manual=True):
        driver = object.__new__(Demonstration)
        driver.manual_ui = manual; driver.args = SimpleNamespace(case='normal')
        driver.root = Path(directory); driver.directory = Path(directory)
        driver.run_id = driver.session = None; driver.first_position = None
        driver.seq_start = 0; driver.ui_boundary_wall = None; driver.details = {}; driver.interrupted = False
        driver.state = lambda: state
        driver.record = lambda *args, **kwargs: None
        calls = []
        def api(path='/api/state', arguments=None, facade=False):
            calls.append((path, arguments))
            return {'new_session': 'session01'} if arguments else copy.deepcopy(self.current_facade)
        self.current_facade = facade; driver.api = api
        return driver, calls

    def test_manual_preflight_freezes_before_input_without_start_or_injection(self):
        state, facade = self.fixture()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory, patch('mhs.smart_acceptance.ledger_rows', return_value=[{'seq': 3, 'type': 'session/start', 'data': {}}]):
            driver, calls = self.driver(directory, state, facade)
            driver.preflight(); driver.initial_injections(); driver.conditional_injections(state)
            self.assertEqual(driver.session, 'session01'); self.assertEqual(driver.seq_start, 3)
            self.assertTrue(all(arguments is None for _, arguments in calls), 'manual mode must only read during startup/events')
            with self.assertRaisesRegex(RuntimeError, 'must_not_inject'):
                driver.inject('insert_specimen', {}, 'forbidden')

    def test_default_preflight_keeps_scripted_start_behavior(self):
        state, facade = self.fixture()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory, patch('mhs.smart_acceptance.ledger_rows', return_value=[]):
            driver, calls = self.driver(directory, state, facade, manual=False); driver.preflight()
            starts = [args for path, args in calls if path == '/api/demo/command']
            self.assertEqual(len(starts), 1); self.assertEqual(starts[0]['action'], 'smart_start')

    def test_watch_claim_can_freeze_new_session_before_events_and_without_writes(self):
        state, facade = self.fixture(); state['smart_ward']['watch'] = {'enabled': True, 'session_id': 'session01'}
        facade['operations'] = [{'action': 'smart_start', 'status': 'completed', 'wall_time': 101,
                                 'new_session': 'session01', 'expected_run_id': 'run01'}]
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory, patch('mhs.smart_acceptance.ledger_rows', return_value=[]), patch('builtins.print') as output:
            driver, calls = self.driver(directory, state, facade)
            driver.run_id = 'run01'; driver.ui_boundary_wall = 100
            driver.observation_end = float('inf'); driver.wait_for_ui_start()
            self.assertEqual(driver.ui_boundary_wall, 100, 'newly claimed session must retain pre-click wall boundary')
            self.assertTrue(any('WAITING_UI_EVENTS' in str(call) for call in output.call_args_list))
            self.assertTrue(all(arguments is None for _, arguments in calls))

    def test_late_boundary_rejects_existing_model_input_and_environment(self):
        state, facade = self.fixture()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            driver, _ = self.driver(directory, state, facade)
            for row in ({'seq': 1, 'type': 'request/sent', 'data': {}},
                        {'seq': 1, 'type': 'user/message', 'data': {}},
                        {'seq': 1, 'type': 'inbox', 'data': {'items': [{'kind': 'follow_up'}]}}):
                with patch('mhs.smart_acceptance.ledger_rows', return_value=[row]), self.assertRaisesRegex(RuntimeError, 'after_model_input'):
                    driver.freeze_ui_boundary(state)
            state['smart_ward']['operations']['already-dark'] = {}
            with self.assertRaisesRegex(RuntimeError, 'after_environment_event'): driver.freeze_ui_boundary(state)

    def test_manual_mode_is_normal_only_and_start_wait_is_bounded(self):
        with self.assertRaisesRegex(ValueError, 'normal_only'):
            Demonstration(SimpleNamespace(case='urgent', manual_ui=True))
        state, facade = self.fixture()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory, patch('builtins.print'):
            driver, calls = self.driver(directory, state, facade); driver.observation_end = 0
            with self.assertRaisesRegex(TimeoutError, 'within_budget'): driver.wait_for_ui_start()
            self.assertEqual(calls, [], 'expired wait cannot start watch or inject any world change')

    def test_ui_gate_requires_button_ack_identity_and_real_detected_demand(self):
        state, facade = self.proof_fixture()
        self.assertTrue(manual_ui_evidence(state, facade, 'session01', 100)['passed'])
        for change in [lambda s, f: f['operations'][0].update(wall_time=99),
                       lambda s, f: f['operations'][1].update(wall_time=100.5),
                       lambda s, f: f['operations'][1].update(session_id='old-session'),
                       lambda s, f: s['smart_ward']['operations']['insert01']['ack'].update(run_id='old-run'),
                       lambda s, f: s['smart_ward']['operations']['insert01']['ack'].update(epoch='old-epoch'),
                       lambda s, f: s['smart_ward']['operations']['dark01']['ack'].update(status='failed'),
                       lambda s, f: s['smart_ward']['operations']['dark01']['ack']['result'].update(actual_percent=60),
                       lambda s, f: s['smart_ward'].update(incidents=[]),
                       lambda s, f: s['smart_ward']['operations']['dark01'].update(origin='agent'),
                       lambda s, f: f['operations'].append({'action': 'smart_event', 'kind': 'set_receiver', 'wall_time': 103, 'expected_run_id': 'run01'})]:
            state, facade = self.proof_fixture(); change(state, facade)
            self.assertFalse(manual_ui_evidence(state, facade, 'session01', 100)['passed'])


if __name__ == '__main__':
    unittest.main()
