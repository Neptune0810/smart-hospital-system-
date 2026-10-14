"""Pure classifier/observer guards only; these tests never call a live service."""
import copy
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from . import smart_receiver_timeout as timeout


def stamp(value):
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()


def row(seq, kind, data, at=900):
    return {'seq': seq, 'ts': stamp(at), 'type': kind, 'data': data}


def tool(seq, name, args, body=None, at=900, structured=None):
    call = row(seq, 'tool/call', {'name': name, 'toolUseId': 'tool'+str(seq), 'input': args}, at)
    data = {'toolUseId': 'tool'+str(seq), 'isError': False,
            'content': [{'type': 'text', 'text': json.dumps({'ok': True, 'result': body})}]}
    if structured is not None:
        data['structured'] = structured
    return [call, row(seq+1, 'tool/result', data, at+.01)]


def fixture():
    identity = {'run_id': 'r', 'epoch': 'e', 'specimen_id': 's', 'order_id': 'o', 'incident_id': 'i'}
    boundary = {'run_id': 'r', 'epoch': 'e', 'sample_seq': 10, 'sim_time': 99,
                'wall_time': 850, 'ledger_seq': 14, 'first_false_sample_wall': 860, 'timeout_wall': 999.9}
    incident = {'incident_id': 'i', 'kind': 'specimen_ready', 'specimen_id': 's', 'status': 'unresolved'}
    state = {'run_id': 'r', 'sim_time': 250, 'pose_fresh': True, 'pose_wall_time': 1000,
        'physical_pose': {'wall_time': 1000, 'position': [0, 0, 0]},
        'pose': {'speed': 0, 'angular_speed': 0}, 'stopped_confirmed': True,
        'stations': {'lab': {'x': 0, 'y': 0}}, 'at_stations': ['lab'],
        'navigation': {'status': 'succeeded'},
        'navigation_tasks': {'b': {'goal_station': 'station_b', 'status': 'succeeded', 'route_id': 'rb'},
                             'l': {'goal_station': 'lab', 'status': 'succeeded', 'route_id': 'rl'}},
        'orders': {'o': {'order_id': 'o', 'specimen_id': 's', 'incident_id': 'i', 'status': 'failed',
            'reason': timeout.REASON, 'loaded': True}},
        'cargo_count': 1, 'receivers': {'lab': False}, 'receiver_unavailable_since': {'lab': 100},
        'mhs_outcome': 'incomplete', 'mhs_budget': {'decisions': 2, 'start_wall': 800, 'end_wall': 1000},
        'events': [{'kind': 'order_failed', 'order_id': 'o', 'reason': timeout.REASON, 'sim_time': 250, 'wall_time': 999.9}],
        'smart_ward': {'epoch': 'e', 'episode': 1, 'verification_event_version': 8, 'scene_fresh': True,
            'last_verification': {'episode': 1, 'event_version': 8}, 'recovery_required': True,
            'model_request_ids': ['inference1', 'inference2'], 'incidents': [incident],
            'specimens': {'s': {'location': 'robot'}},
            'devices': {'hospital-cabinet-lab': {'fresh': True, 'available': False, 'seq': 30, 'wall_time': 1000, 'sim_time': 250}},
            'scene_sample': {'run_id': 'r', 'epoch': 'e', 'seq': 30, 'wall_time': 1000, 'sim_time': 250},
            'operations': {'p': {'status': 'completed', 'command': {'kind': 'handoff', 'arguments': {'order_id': 'o', 'cabinet_id': 'hospital-cabinet-b'}},
                'ack': {'status': 'completed', 'run_id': 'r', 'epoch': 'e', 'operation_id': 'p',
                    'result': {'transfer_id': 't', 'specimen_id': 's', 'order_id': 'o', 'cabinet_id': 'hospital-cabinet-b', 'location': 'robot'}}}},
            'transfers': {'t': {'status': 'committed', 'specimen_id': 's',
                'direction': 'pickup', 'operation_id': 'p', 'location': 'robot'}}}}
    observation = {'run_id': 'r', 'epoch': 'e', 'seq': 30, 'wall_time': 1000,
        'physical_robot': {'available': True, 'wall_time': 1000, 'stopped_samples': 3, 'speed_mps': 0, 'angular_speed_rad_s': 0},
        'specimens': {'s': {'location': 'robot'}}, 'devices': {'hospital-cabinet-lab': {'specimens': []}},
        'transfers': {'t': {'specimen_id': 's', 'location': 'robot', 'operation_id': 'p'}}}
    rows = [row(1, 'effect/intent', {'kind': 'inference', 'effectId': 'inference1'}, 801),
            row(2, 'request/sent', {}, 801)]
    rows += tool(3, 'hospital_create_specimen_delivery', {'incident_id': 'i'}, {'orders': [{'order_id': 'o', 'specimen_id': 's'}]}, 802)
    rows += tool(5, 'hospital_publish_service_plan', {}, {}, 803)
    rows += tool(7, 'hospital_get_route_options', {}, {}, 804)
    rows += tool(9, 'call_device', {'device': 'hospital-robot-01', 'tool': 'navigate_route', 'arguments': {'route_id': 'rb'}}, {}, 805)
    rows += tool(11, 'call_device', {'device': 'hospital-cabinet-b', 'tool': 'handoff_order', 'arguments': {'order_id': 'o'}}, {}, 830)
    rows += tool(13, 'call_device', {'device': 'hospital-robot-01', 'tool': 'navigate_route', 'arguments': {'route_id': 'rl'}}, {}, 840)
    rows += tool(15, 'hospital_defer_order', {'order_id': 'o'}, {'order': {'status': 'deferred'}}, 865)
    args = {'device': 'hospital-robot-01', 'until': {'state': 'delivery_wait_lab', 'eq': True}, 'timeout': 300}
    rows += tool(17, 'watch_device', args, None, 870, {**args, 'watch': 'w1'})
    rows += [row(19, 'user/message', {'content': [{'type': 'text', 'text': '[AgnesHub] hospital-robot-01: delivery_wait_lab = true.'}]}, 999.91)]
    rows += tool(20, 'hospital_get_state', {}, {'run_id': 'r', 'mhs_outcome': 'incomplete', 'orders': state['orders']}, 999.92)
    rows += [row(22, 'effect/intent', {'kind': 'inference', 'effectId': 'inference2'}, 999.94)]
    verification = {'run_id': 'r', 'episode': 1, 'verification_event_version': 8, 'ready_to_finalize': True,
        'pending': [], 'specimen_delivered': 0, 'unresolved': [incident], 'incidents': [incident]}
    rows += tool(23, 'hospital_verify_services', {}, verification, 999.96)
    rows += [row(25, 'assistant/message', {'content': [{'type': 'text', 'text': '标本未交付，仍保留在机器人上，需要人工处理。'}]}, 999.98),
             row(26, 'turn/end', {'reason': 'completed'}, 1000.1)]
    return state, observation, rows, identity, boundary


class NativeWatchTests(unittest.TestCase):
    def proof(self, rows, boundary=None):
        boundary = boundary or fixture()[4]
        watches = timeout.receiver_watch_records(rows)
        return timeout.native_watch_linkage(rows, watches, boundary['timeout_wall'], boundary['first_false_sample_wall'])

    def test_unique_accepted_structured_watch_exact_merged_message(self):
        rows = fixture()[2]
        rows[18]['data']['content'][0]['text'] += '\nhospital-robot-01: another job ended.'
        proof = self.proof(rows)
        self.assertTrue(proof['passed'])
        self.assertEqual(proof['watch']['data']['watch'], 'w1')
        self.assertFalse(proof['native_event_id_directly_observed'])
        self.assertEqual(proof['proof_level'], 'pinned_official_text_and_unique_accepted_watch')

    def test_wrong_text_timeout_stale_or_early_message_does_not_match(self):
        for change in (lambda rows: rows[18]['data']['content'][0].update(text='[AgnesHub] hospital-robot-01: waited 300 s and delivery_wait_lab = true did not happen.'),
                       lambda rows: rows[18]['data']['content'][0].update(text='[设备事件] hospital-robot-01: delivery_wait_lab = true.'),
                       lambda rows: rows[18]['data']['content'][0].update(text='[AgnesHub] hospital-robot-01: delivery_wait_lab == true.'),
                       lambda rows: rows[18].update(ts=stamp(999)),
                       lambda rows: rows[18].update(ts=stamp(1170))):
            rows = fixture()[2]; change(rows)
            self.assertFalse(self.proof(rows)['passed'])

    def test_exact_note_and_structured_arguments_are_required(self):
        rows = fixture()[2]
        rows[16]['data']['input']['note'] = '等待检验接收'
        rows[17]['data']['structured']['note'] = '等待检验接收'
        self.assertFalse(self.proof(rows)['passed'])
        rows[18]['data']['content'][0]['text'] = '[AgnesHub] hospital-robot-01: delivery_wait_lab = true (等待检验接收).'
        self.assertTrue(self.proof(rows)['passed'])
        rows[17]['data']['structured']['watch'] = None
        self.assertFalse(self.proof(rows)['passed'])

    def test_multiple_indistinguishable_watches_are_na_but_cancellation_disambiguates(self):
        rows = fixture()[2]
        other = tool(180, 'watch_device', copy.deepcopy(rows[16]['data']['input']), None, 880,
                     {**copy.deepcopy(rows[17]['data']['structured']), 'watch': 'w2'})
        # Sequence, not list position, determines matching; preserve actual order.
        rows[18:18] = other
        for index, item in enumerate(rows, 1): item['seq'] = index
        self.assertEqual(self.proof(rows)['reason'], 'multiple_indistinguishable_live_accepted_watches')
        canceled = tool(185, 'unwatch_device', {'watch': 'w2'}, {}, 900)
        rows[20:20] = canceled
        for index, item in enumerate(rows, 1): item['seq'] = index
        self.assertTrue(self.proof(rows)['passed'])

    def test_before_false_registered_or_already_consumed_watch_cannot_cover_timeout(self):
        rows = fixture()[2]
        boundary = fixture()[4]; boundary['first_false_sample_wall'] = 871
        self.assertFalse(self.proof(rows, boundary)['passed'])
        early = row(18.5, 'user/message', copy.deepcopy(rows[18]['data']), 880)
        rows.insert(18, early)
        for index, item in enumerate(rows, 1): item['seq'] = index
        self.assertFalse(self.proof(rows)['passed'])

    def test_only_full_pinned_text_can_replace_missing_structured_data(self):
        rows = fixture()[2]; rows[17]['data'].pop('structured')
        rows[17]['data']['content'][0]['text'] = 'Watching hospital-robot-01 as w1. If nothing else needs doing now, end your turn: this conversation is woken with a message when it happens or after the timeout. Do not wait, sleep or poll with other tools.'
        self.assertTrue(self.proof(rows)['passed'])
        rows[17]['data']['content'][0]['text'] = 'Watching hospital-robot-01 as w1.'
        self.assertFalse(self.proof(rows)['passed'])


class IncompleteClassifierTests(unittest.TestCase):
    def proof(self, state, observation, rows, identity, boundary):
        native = timeout.native_watch_linkage(rows, timeout.receiver_watch_records(rows), boundary['timeout_wall'], boundary['first_false_sample_wall'])
        with patch('mhs.smart_receiver_timeout.time.time', return_value=1000), patch('mhs.smart_navigation.time.time', return_value=1000):
            return timeout.incomplete_evidence(state, observation, rows, identity, boundary, 100, native)

    def test_complete_boundary_is_expected_incomplete_never_delivery_success(self):
        proof = self.proof(*fixture())
        self.assertTrue(proof['passed'], proof['checks'])
        self.assertEqual(proof['business_outcome'], 'incomplete')
        self.assertFalse(proof['delivered_acceptance_passed'])

    def test_149_point_999_or_wrong_reason_or_timer_cannot_pass(self):
        for change in (lambda s: s.update(sim_time=249.999),
                       lambda s: s['events'][0].update(sim_time=249.999),
                       lambda s: s['orders']['o'].update(reason='user_stop'),
                       lambda s: s['receiver_unavailable_since'].update(lab=101)):
            values = fixture(); change(values[0])
            self.assertFalse(self.proof(*values)['passed'])

    def test_receipt_owner_or_unknown_handoff_invalidate_incomplete_evidence(self):
        changes = (lambda s, o: s['orders']['o'].update(receipt={'receipt_id': 'r'}),
                   lambda s, o: o['devices']['hospital-cabinet-lab'].update(specimens=['s']),
                   lambda s, o: o['specimens']['s'].update(location='hospital-cabinet-b'),
                   lambda s, o: s['smart_ward']['operations']['p'].update(status='unknown'))
        for change in changes:
            values = fixture(); change(values[0], values[1])
            self.assertFalse(self.proof(*values)['passed'])

    def test_cleanup_endwall_operator_stop_or_stale_verify_never_manufacture_completion(self):
        for change in (lambda s: s.update(operator_stopped={'reason': 'user_stop'}),
                       lambda s: s['mhs_budget'].pop('end_wall'),
                       lambda s: s['smart_ward'].update(verification_event_version=9),
                       lambda s: s['smart_ward'].update(model_request_ids=['inference1']),
                       lambda s: s['smart_ward'].update(scene_fresh=False)):
            values = fixture(); change(values[0])
            self.assertFalse(self.proof(*values)['passed'])

    def test_defer_wait_reread_final_report_and_terminal_are_independent_gates(self):
        for missing in ('hospital_defer_order', 'watch_device', 'hospital_get_state', 'hospital_verify_services', 'assistant/message', 'turn/end'):
            values = fixture()
            calls = {r['data'].get('toolUseId') for r in values[2] if r.get('data', {}).get('name') == missing}
            values[2][:] = [r for r in values[2] if r.get('data', {}).get('name') != missing and r.get('data', {}).get('toolUseId') not in calls and r['type'] != missing]
            self.assertFalse(self.proof(*values)['passed'], missing)

    def test_missing_actual_agent_route_or_pickup_ack_is_not_replaced_by_state(self):
        for change in (lambda s, o: s['navigation_tasks']['b'].update(route_id='not-called'),
                       lambda s, o: s['smart_ward']['operations']['p']['ack'].update(status='failed'),
                       lambda s, o: o['transfers']['t'].update(location='hospital-cabinet-b')):
            values = fixture(); change(values[0], values[1])
            self.assertFalse(self.proof(*values)['passed'])


class BudgetAndObserverTests(unittest.TestCase):
    def report(self):
        cases = []
        for name, station, duration in (('arrival_b', 'station_b', 772), ('arrival_lab', 'lab', 800)):
            nav = {'station': station, 'task_id': name, 'started_wall': 1000,
                   'before': {'sim_time': 0, 'physical_pose': {'position': [1, 2, 0]}}}
            state = {'sim_time': 150, 'navigation_tasks': {name: {'status': 'succeeded', 'goal_station': station}}}
            cases.append({'case': name, 'status': 'passed', 'details': {'navigation': nav, 'state': state,
                'hub_result': {'status': 'done'}, 'confirmed_wall': 1000+duration}})
        return {'cases': cases}

    def test_current_rtf_and_original_budget_can_refuse_without_extending_deadline(self):
        estimate = timeout.timing_estimate(self.report(), .2, 3600, [1, 2, 0])
        self.assertTrue(estimate['go']); self.assertEqual(estimate['legs'][0]['forecast_wall_seconds'], 772)
        for rtf, available in ((.05, 3600), (.2, 2000), (0, 3600), (float('nan'), 3600)):
            self.assertFalse(timeout.timing_estimate(self.report(), rtf, available, [1, 2, 0])['go'])

    def test_failed_missing_or_changed_start_history_is_nogo(self):
        report = self.report(); report['cases'][1]['status'] = 'failed'
        self.assertFalse(timeout.timing_estimate(report, .3, 3600)['go'])
        self.assertEqual(timeout.timing_estimate(self.report(), .3, 3600, [2, 2, 0])['reason'], 'historical_b_start_differs_from_current_start')

    def test_actual_delivery_or_prepared_receiver_race_is_not_backdated(self):
        state, observation, _, identity, _ = fixture()
        state['orders']['o'].update(status='delivered', receipt={'id': 'actual'})
        with self.assertRaisesRegex(RuntimeError, 'window_missed_already_delivered'):
            timeout.lab_arrival_window(state, observation, 's', 'r', 'e')

    def test_lab_window_requires_real_fresh_still_arrival_and_robot_ownership(self):
        state, observation, _, _, _ = fixture()
        state['orders']['o'].update(status='onboard')
        state['receivers']['lab'] = True; state['receiver_unavailable_since'] = {}
        with patch('mhs.smart_navigation.time.time', return_value=1000):
            self.assertEqual(timeout.lab_arrival_window(state, observation, 's', 'r', 'e')['task_id'], 'l')
            observation['physical_robot']['stopped_samples'] = 2
            self.assertIsNone(timeout.lab_arrival_window(state, observation, 's', 'r', 'e'))
        state['orders']['o'].update(status='onboard', receipt=None)
        state['smart_ward']['operations']['receive'] = {'status': 'pending', 'command': {'kind': 'handoff', 'arguments': {'cabinet_id': 'hospital-cabinet-lab'}}}
        with self.assertRaisesRegex(RuntimeError, 'window_missed_receiver_handoff'):
            timeout.lab_arrival_window(state, observation, 's', 'r', 'e')

    def test_invalid_environment_or_second_pause_is_refused_before_api(self):
        driver = object.__new__(timeout.ReceiverTimeout)
        driver.environment_commands = 2; driver.pause_attempted = True; driver.restore_attempted = False
        driver.state = Mock(); driver.api = Mock()
        for kind, args in (('navigate_route', {}), ('set_receiver', {'available': False}),
                           ('insert_specimen', {'cabinet_id': 'hospital-cabinet-c', 'priority': 'normal'})):
            with self.assertRaisesRegex(RuntimeError, 'scope_exceeded'):
                driver.command(kind, args, 'forbidden')
        driver.state.assert_not_called(); driver.api.assert_not_called()

    def test_actual_ack_wrong_identity_or_value_consumes_once_and_never_retries(self):
        driver = object.__new__(timeout.ReceiverTimeout)
        driver.environment_commands = 1; driver.pause_attempted = driver.restore_attempted = False
        driver.interrupted = False; driver.observation_end = 50; driver.deadline = 100
        driver.run_id, driver.epoch = 'r', 'e'; driver.injections = []
        driver.state = Mock(return_value={}); driver.record = Mock()
        def api(path, arguments=None):
            if arguments is not None:
                driver.submitted = arguments; return {'status': 'pending'}
            return {'status': 'completed', 'ack': {'run_id': 'r', 'epoch': 'e', 'status': 'completed',
                'operation_id': driver.submitted['operation_id'], 'result': {'available': True}}}
        driver.api = Mock(side_effect=api)
        with patch('mhs.smart_receiver_timeout.time.monotonic', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'ack_value_mismatch'):
                driver.command('set_receiver', {'available': False}, 'disable')
        self.assertEqual(driver.environment_commands, 2)
        self.assertEqual(driver.api.call_count, 2)
        self.assertTrue(driver.pause_attempted)

    def test_restore_failure_still_requests_stop_and_freezes_original_state_first(self):
        state, observation, _, _, _ = fixture()
        state['smart_ward']['watch'] = {'enabled': True}
        state['motion_guard'] = {'armed': False}
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.cache') as directory:
            driver = object.__new__(timeout.ReceiverTimeout)
            driver.began = True; driver.directory = Path(directory); driver.root = Path(directory)
            driver.session = 'session'; driver.seq_start = 0; driver.proof = {'passed': True}
            driver.run_id, driver.epoch, driver.specimen_id, driver.order_id = 'r', 'e', 's', 'o'
            driver.pause_attempted = True; driver.deadline = 100
            driver.state = Mock(side_effect=lambda: copy.deepcopy(state))
            observations = [copy.deepcopy(observation) for _ in range(4)]
            for index, value in enumerate(observations): value['seq'] = 30+index
            driver.observation = Mock(side_effect=observations)
            driver.command = Mock(side_effect=RuntimeError('actual_restore_ack_unknown'))
            def api(path, arguments=None, facade=False):
                if arguments:
                    state['operator_stopped'] = {'reason': 'user_stop'}
                    state['smart_ward']['watch']['enabled'] = False
                return {}
            driver.api = Mock(side_effect=api)
            with patch('mhs.smart_receiver_timeout.ledger_rows', return_value=[]), patch('mhs.smart_receiver_timeout.time.monotonic', return_value=0), patch('mhs.smart_receiver_timeout.time.sleep'), patch('mhs.smart_navigation.time.time', return_value=1000):
                cleanup = driver.cleanup()
            self.assertTrue(cleanup['physical_stop_confirmed'])
            self.assertTrue(cleanup['final_cargo_retained'])
            self.assertIn('restore_error', cleanup)
            before = json.loads((Path(directory)/'pre-cleanup-state.json').read_text(encoding='utf-8'))
            self.assertNotIn('operator_stopped', before['state'])
            self.assertTrue(before['state']['smart_ward']['watch']['enabled'])
            calls = [call for call in driver.api.call_args_list if call.args[0] == '/api/demo/command']
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].args[1]['action'], 'stop')


if __name__ == '__main__':
    unittest.main()
