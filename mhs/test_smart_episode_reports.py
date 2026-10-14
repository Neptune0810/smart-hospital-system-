"""Pure business/storage tests; fake feedback is not Isaac or Agent evidence."""
import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


PROJECT = next(parent for parent in Path(__file__).resolve().parents
               if (parent / 'mhs/engine_v2.py').is_file() and (parent / 'configs/stations.json').is_file())
sys.path.insert(0, str(PROJECT))
from mhs import test_smart_ward as original_tests

MODULE = 'mhs._episode_report77_candidate'
if MODULE not in sys.modules:
    spec = importlib.util.spec_from_file_location(MODULE, Path(__file__).with_name('smart_ward.py'))
    candidate = importlib.util.module_from_spec(spec)
    sys.modules[MODULE] = candidate
    spec.loader.exec_module(candidate)
ward = sys.modules[MODULE]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EpisodeReportsTests(unittest.TestCase):
    def setUp(self):
        self.wall = 1000.
        self.clock = patch.object(ward.time, 'time', side_effect=lambda: self.wall)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.fixture = original_tests.SmartWardTests(methodName='runTest')
        with patch.object(original_tests, 'SmartWardV1', ward.SmartWardV1):
            self.fixture.setUp()
        self.engine = self.fixture.engine
        self.fixture.start()

    def tearDown(self):
        self.fixture.tearDown()

    def completed_batch(self, sid, wall_seconds, path_delta, requests, extra_sim=0):
        # Existing business fixture deliberately uses synthetic scene/pose/ACK
        # feedback. This test only checks reporting denominators and persistence.
        self.wall += 1
        self.fixture.place('station_b')
        order = self.fixture.sample(sid=sid)
        budget = copy.deepcopy(self.engine.s['mhs_budget'])
        op = self.engine.device_action('hospital-cabinet-b', 'handoff_order',
            {'order_id': order['order_id'], 'operation_id': 'pickup-' + sid})
        self.fixture.handoff_ack(op)
        self.wall += wall_seconds
        self.fixture.place('lab')
        self.fixture.observe()
        op = self.engine.device_action('hospital-cabinet-lab', 'handoff_order',
            {'order_id': order['order_id'], 'operation_id': 'receive-' + sid})
        self.fixture.handoff_ack(op)
        if extra_sim:
            pose = self.engine.pose
            self.engine.telemetry(pose['x'], pose['y'], pose['yaw'], 0, self.engine.s['sim_time'] + extra_sim)
            self.fixture.observe()
        self.engine.s['path_length'] += path_delta  # Explicit synthetic counter fixture.
        for request in requests:
            self.engine.record_model_request(request, self.engine.s['run_id'], 'session-current')
        self.assertTrue(self.engine.s['mhs_finished'])
        self.assertTrue(self.engine.verify_services()['ready_to_finalize'])
        return order, budget

    def finalize(self):
        self.fixture.consume_notifications()
        return self.engine.finalize_episode(self.engine.s['run_id'], 'session-current')

    def path(self, result):
        return self.engine.data_dir / result['report_ref']['path']

    def read(self, result):
        return json.loads(self.path(result).read_bytes())

    def test_two_batches_have_separate_order_receipt_model_and_time_path_denominators(self):
        first_order, first_budget = self.completed_batch('first', 20, 7, ['req-a', 'req-b'])
        first = self.finalize()
        first_path, first_raw = self.path(first), self.path(first).read_bytes()
        first_body = self.read(first)
        self.assertEqual(first_body['model_request_ids'], ['req-a', 'req-b'])
        self.assertEqual(first_body['smart_ward']['model_calls'], 2)
        self.assertEqual(first_body['smart_ward']['order_ids'], [first_order['order_id']])
        self.assertEqual((first_body['wall_seconds'], first_body['path_length']), (20, 7))
        self.assertEqual(first_body['model_request_count'], 2)
        self.assertIsNone(first_body['token_usage'])
        self.assertIsNone(first_body['actual_monetary_cost'])
        second_order, second_budget = self.completed_batch('second', 30, 13, ['req-c', 'req-d', 'req-e'], extra_sim=6)
        second = self.finalize()
        body = self.read(second)
        self.assertEqual((first['episode'], second['episode']), (1, 2))
        self.assertNotEqual(first_budget['start_wall'], second_budget['start_wall'])
        self.assertEqual(body['order_ids'], [second_order['order_id']])
        self.assertEqual(body['total'], 1)
        self.assertEqual(body['delivered'], 1)
        self.assertEqual(body['receipts'][0]['order_id'], second_order['order_id'])
        self.assertEqual((body['wall_seconds'], body['path_length']), (30, 13))
        self.assertNotEqual(body['elapsed_sim'], first_body['elapsed_sim'])
        self.assertEqual(body['model_request_ids'], ['req-c', 'req-d', 'req-e'])
        self.assertEqual(body['smart_ward']['model_calls'], 3)
        self.assertNotEqual(body['model_request_ids_sha256'], first_body['model_request_ids_sha256'])
        self.assertEqual(body['run_totals']['total'], 2)
        self.assertEqual(body['run_totals']['delivered'], 2)
        self.assertEqual(body['run_totals']['metrics_scope'], 'cumulative_run_counts_only')
        self.assertNotIn('receipts', body['run_totals'])
        self.assertEqual(first_path.read_bytes(), first_raw)
        self.assertEqual(sha(first_path), first['report_ref']['sha256'])
        self.assertEqual((self.engine.data_dir / self.engine.s['run_id'] / 'report.json').read_bytes(), self.path(second).read_bytes())
        self.assertEqual(len(self.engine.s['orders']), 2)
        self.assertEqual(len(self.engine.s['segment_order_ids']), 2)  # Real history was not cleared.

    def test_unfinalized_new_event_preserves_the_original_budget_and_model_ids(self):
        self.fixture.low_light()
        self.engine.record_model_request('one', self.engine.s['run_id'], 'session-current')
        original = copy.deepcopy(self.engine.s['mhs_budget'])
        episode = self.engine.s['smart_ward']['episode']
        self.fixture.devices['hospital-sensor-303']['relative_illumination'] = 28
        for _ in range(3):
            self.fixture.observe()
        self.assertEqual(self.engine.s['smart_ward']['episode'], episode)
        self.assertEqual(self.engine.s['mhs_budget'], original)
        self.assertEqual(self.engine.s['smart_ward']['model_request_ids'], ['one'])
        self.assertEqual(len(self.engine._episode_incidents()), 2)

    def test_same_episode_finalize_reuses_original_ref_bytes_despite_new_feedback(self):
        self.completed_batch('first', 20, 7, ['request'])
        first = self.finalize()
        raw = self.path(first).read_bytes()
        self.wall += 1
        self.fixture.observe()
        self.engine.s['smart_ward']['verification_event_version'] += 1
        self.engine.record_model_request('outside-closed-budget', self.engine.s['run_id'], 'session-current')
        repeated = self.finalize()
        self.assertEqual(repeated, first)
        self.assertEqual(self.path(first).read_bytes(), raw)
        self.assertEqual(self.engine.s['smart_ward']['model_request_ids'], ['request'])
        self.assertEqual(len(self.engine.s['smart_ward']['episode_reports']), 1)

    def test_repeat_finalize_after_stop_keeps_original_seal_and_stop_summary(self):
        self.completed_batch('first', 20, 7, ['request'])
        first = self.finalize()
        original = self.path(first).read_bytes()
        self.wall += 1
        self.fixture.place('lab')
        self.fixture.observe()
        self.engine.stop_run('stop-after-finalize', self.engine.s['run_id'])
        latest = self.path(first).parent / 'report.json'
        stopped_raw = latest.read_bytes()
        self.assertNotEqual(stopped_raw, original)
        self.assertEqual(self.finalize(), first)
        self.assertEqual(self.path(first).read_bytes(), original)
        self.assertEqual(latest.read_bytes(), stopped_raw)

    def test_original_bytes_are_fsynced_before_sqlite_publishes_closed_budget(self):
        self.completed_batch('first', 20, 7, ['request'])
        original_prepare = ward._prepare_report_bytes
        stages = []
        def inspect(path, raw):
            original_prepare(path, raw)
            with closing(sqlite3.connect(self.engine.data_dir / 'state.sqlite')) as reader:
                saved = json.loads(reader.execute('SELECT body FROM snapshots WHERE id=1').fetchone()[0])
            stages.append((path.read_bytes() == raw,
                           saved['mhs_budget'].get('end_wall'),
                           saved['smart_ward'].get('episode_reports')))
        with patch.object(ward, '_prepare_report_bytes', side_effect=inspect):
            result = self.finalize()
        self.assertEqual(stages, [(True, None, None)])
        self.assertEqual(sha(self.path(result)), result['report_ref']['sha256'])

    def test_crash_after_preparation_leaves_unindexed_original_and_blocks_rerender(self):
        self.completed_batch('first', 20, 7, ['request'])
        original_prepare = ward._prepare_report_bytes
        class SimulatedCrash(BaseException):
            pass
        captured = []
        def crash(path, raw):
            original_prepare(path, raw)
            captured.append((path, raw))
            raise SimulatedCrash()
        with patch.object(ward, '_prepare_report_bytes', side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                self.finalize()
        path, raw = captured[0]
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(self.engine.s['smart_ward'].get('episode_reports'))
        self.assertNotIn('end_wall', self.engine.s['mhs_budget'])
        self.engine.db.close()
        self.engine = ward.SmartWardV1(self.fixture.root, self.fixture.stations, require_sim_truth=True)
        self.fixture.engine = self.engine
        self.engine.backend = original_tests.Backend()
        self.engine.guard.lease(True)
        self.fixture.place('lab')
        self.fixture.observe()
        with self.assertRaisesRegex(ValueError, 'unindexed_episode_report_requires_reconciliation'):
            self.finalize()
        with self.engine._atomic():
            self.assertFalse(self.engine._new_episode())
        self.assertTrue(self.engine.s['smart_ward']['report_seal_pending'])
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse((path.parent / 'episode-1-report.json').exists())

    def test_report_ref_is_small_and_raw_model_ids_are_only_in_sealed_evidence(self):
        identities = ['request-' + str(i) for i in range(10)]
        self.completed_batch('first', 20, 7, identities)
        result = self.finalize()
        ref = self.engine.s['smart_ward']['episode_reports']['1']
        self.assertLess(len(json.dumps(ref)), 2500)
        self.assertNotIn('model_request_ids', ref)
        self.assertNotIn('report', ref)
        self.assertEqual(ref['model_request_count'], 10)
        self.assertEqual(self.read(result)['model_request_ids'], identities)
        self.assertEqual(self.path(result).stat().st_nlink, 1)
        self.assertFalse((self.path(result).parent / ref['prepared_name']).exists())

    def test_legacy55_report_is_never_overwritten_or_migrated(self):
        self.completed_batch('first', 20, 7, ['request'])
        target = self.engine.data_dir / self.engine.s['run_id'] / 'report.json'
        original = b'{"protocol":"smart-ward-v1","legacy55":true}\n'
        target.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'legacy_or_unowned'):
            self.finalize()
        self.assertEqual(target.read_bytes(), original)
        self.assertNotIn('end_wall', self.engine.s['mhs_budget'])
        self.assertFalse(self.engine.s['smart_ward'].get('episode_reports'))

    def test_failed_publish_retains_candidate_and_new_scene_box_for_later_redetection(self):
        self.completed_batch('first', 20, 7, ['request'])
        original_start = self.engine.s['mhs_budget']['start_wall']
        with patch.object(ward.os, 'link', side_effect=OSError('synthetic publication failure')):
            with self.assertRaisesRegex(OSError, 'publication failure'):
                self.finalize()
        ref = copy.deepcopy(self.engine.s['smart_ward']['episode_reports']['1'])
        self.assertEqual(ref['status'], 'unknown')
        prepared = self.engine.data_dir / self.engine.s['run_id'] / ref['prepared_name']
        original = prepared.read_bytes()
        self.assertEqual(sha(prepared), ref['sha256'])
        self.assertEqual(self.engine.s['mhs_budget']['start_wall'], original_start)
        self.fixture.specimens['later'] = {'specimen_id': 'later', 'location': 'hospital-cabinet-b',
            'sealed': True, 'priority': 'normal', 'deadline_sim': self.engine.s['sim_time'] + 900}
        for _ in range(3):
            self.fixture.observe()  # Must not throw, create an order or lose the box.
        self.assertEqual(self.engine.s['smart_ward']['episode'], 1)
        self.assertIn('later', self.engine.s['smart_ward']['specimens'])
        self.assertFalse(any(i.get('specimen_id') == 'later' for i in self.engine.s['smart_ward']['incidents'].values()))
        self.assertEqual(len(self.engine.s['orders']), 1)
        repeated = self.finalize()
        self.assertEqual(self.path(repeated).read_bytes(), original)
        self.fixture.observe()
        self.assertEqual(self.engine.s['smart_ward']['episode'], 2)
        self.assertEqual(sum(i.get('specimen_id') == 'later' for i in self.engine.s['smart_ward']['incidents'].values()), 1)
        self.assertEqual(self.engine.s['smart_ward']['specimens']['later']['location'], 'hospital-cabinet-b')

    def test_batch2_disk_failure_preserves_batch1_seal_and_latest_bytes(self):
        self.completed_batch('first', 20, 7, ['request-1'])
        first = self.finalize()
        previous = self.path(first).read_bytes()
        latest = self.path(first).parent / 'report.json'
        self.completed_batch('second', 30, 13, ['request-2'])
        with patch.object(ward.os, 'link', side_effect=OSError('synthetic second seal failure')):
            with self.assertRaises(OSError):
                self.finalize()
        self.assertEqual(self.path(first).read_bytes(), previous)
        self.assertEqual(latest.read_bytes(), previous)
        self.assertEqual(self.engine.s['smart_ward']['episode_reports']['2']['status'], 'unknown')
        second = self.finalize()
        self.assertEqual(sha(self.path(second)), second['report_ref']['sha256'])
        self.assertEqual(self.path(first).read_bytes(), previous)

    def test_partial_original_is_preserved_and_never_rerendered_as_success(self):
        self.completed_batch('first', 20, 7, ['request'])
        def partial(path, raw):
            with path.open('xb') as stream:
                stream.write(raw[:11])
            raise OSError('synthetic disk full')
        with patch.object(ward, '_prepare_report_bytes', side_effect=partial):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.finalize()
        ref = self.engine.s['smart_ward']['episode_reports']['1']
        path = self.engine.data_dir / self.engine.s['run_id'] / ref['prepared_name']
        previous = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'digest_mismatch'):
            self.finalize()
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(self.engine.s['smart_ward']['episode_reports']['1']['status'], 'unknown')
        self.assertFalse((path.parent / 'episode-1-report.json').exists())

    def test_latest_write_failure_reports_error_and_retry_uses_sealed_original(self):
        self.completed_batch('first', 20, 7, ['request'])
        with patch.object(ward, '_latest_report_bytes', side_effect=OSError('synthetic latest failure')):
            with self.assertRaisesRegex(OSError, 'latest failure'):
                self.finalize()
        ref = copy.deepcopy(self.engine.s['smart_ward']['episode_reports']['1'])
        self.assertEqual(ref['status'], 'sealed')
        sealed = self.engine.data_dir / ref['path']
        original = sealed.read_bytes()
        self.assertTrue(self.engine.s['smart_ward']['report_seal_pending'])
        self.fixture.specimens['later'] = {'specimen_id': 'later', 'location': 'hospital-cabinet-b',
            'sealed': True, 'priority': 'normal', 'deadline_sim': self.engine.s['sim_time'] + 900}
        self.fixture.observe()
        self.assertEqual(self.engine.s['smart_ward']['episode'], 1)
        self.assertFalse(any(i.get('specimen_id') == 'later'
            for i in self.engine.s['smart_ward']['incidents'].values()))
        result = self.finalize()
        self.assertEqual(result['report_ref']['sha256'], ref['sha256'])
        self.assertEqual(sealed.read_bytes(), original)
        self.assertEqual((sealed.parent / 'report.json').read_bytes(), original)
        self.fixture.observe()
        self.assertEqual(self.engine.s['smart_ward']['episode'], 2)

    def test_report_directory_error_does_not_kill_sampler_or_discard_new_box(self):
        self.completed_batch('first', 20, 7, ['request'])
        original_budget = copy.deepcopy(self.engine.s['mhs_budget'])
        self.fixture.specimens['later'] = {'specimen_id': 'later', 'location': 'hospital-cabinet-b',
            'sealed': True, 'priority': 'normal', 'deadline_sim': self.engine.s['sim_time'] + 900}
        with patch.object(self.engine, '_unindexed_episode_originals', side_effect=PermissionError('synthetic metadata error')):
            self.fixture.observe()
        self.assertIn('later', self.engine.s['smart_ward']['specimens'])
        self.assertEqual(self.engine.s['mhs_budget'], original_budget)
        self.assertEqual(len(self.engine.s['orders']), 1)
        self.assertTrue(self.engine.s['smart_ward']['report_seal_pending'])
        self.assertIn('metadata error', self.engine.s['smart_ward']['report_seal_error'])
        self.fixture.observe()
        self.assertEqual(self.engine.s['smart_ward']['episode'], 1)
        self.assertEqual(sum(i.get('specimen_id') == 'later'
            for i in self.engine.s['smart_ward']['incidents'].values()), 1)
        self.assertEqual(self.engine.s['mhs_budget']['start_wall'], original_budget['start_wall'])
        self.assertFalse(self.engine.s['smart_ward']['report_seal_pending'])

    def test_pending_seal_survives_backend_restart_without_fabricating_a_new_report(self):
        self.completed_batch('first', 20, 7, ['request'])
        with patch.object(ward.os, 'link', side_effect=OSError('synthetic publication failure')):
            with self.assertRaises(OSError):
                self.finalize()
        ref = copy.deepcopy(self.engine.s['smart_ward']['episode_reports']['1'])
        prepared = self.engine.data_dir / self.engine.s['run_id'] / ref['prepared_name']
        original = prepared.read_bytes()
        self.engine.db.close()
        self.engine = ward.SmartWardV1(self.fixture.root, self.fixture.stations, require_sim_truth=True)
        self.fixture.engine = self.engine
        self.engine.backend = original_tests.Backend()
        self.engine.guard.lease(True)
        self.fixture.place('lab')
        self.fixture.observe()
        self.assertFalse(self.engine.s['smart_ward']['watch']['enabled'])
        self.assertEqual(self.engine.s['smart_ward']['episode_reports']['1']['sha256'], ref['sha256'])
        result = self.finalize()
        self.assertEqual(self.path(result).read_bytes(), original)
        self.assertEqual(self.engine.s['smart_ward']['episode'], 1)

    def test_global_ready_gate_is_not_weakened_by_current_episode_reporting(self):
        self.completed_batch('first', 20, 7, ['request'])
        self.engine.s['smart_ward']['incidents']['older-pending'] = {
            'incident_id': 'older-pending', 'episode': 0, 'kind': 'low_light', 'room_id': 'room303', 'status': 'new'}
        report = self.engine.verify_services()
        self.assertEqual(report['total'], 1)
        self.assertEqual(report['pending'], [])
        self.assertFalse(report['ready_to_finalize'])
        with self.assertRaisesRegex(ValueError, 'not_ready'):
            self.finalize()

    def test_original_identity_and_stale_verification_rejections_are_unchanged(self):
        self.completed_batch('first', 20, 7, ['request'])
        for run, session, reason in (('OTHER', 'session-current', 'session_run_mismatch'),
                                     (self.engine.s['run_id'], 'OTHER', 'another_session_controls')):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                self.engine.finalize_episode(run, session)
        self.engine.s['smart_ward']['verification_event_version'] += 1
        with self.assertRaisesRegex(ValueError, 'verification_required'):
            self.finalize()
        self.assertFalse(self.engine.s['smart_ward'].get('episode_reports'))


if __name__ == '__main__':
    unittest.main()
