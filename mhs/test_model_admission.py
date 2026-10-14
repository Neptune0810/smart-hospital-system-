"""Candidate89 pure business/storage qualification; fake feedback is not Agent/Isaac proof."""
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import unittest
from unittest.mock import patch
import uuid

PROJECT = next(p for p in Path(__file__).resolve().parents if (p / 'mhs/engine_v2.py').is_file())
sys.path.insert(0, str(PROJECT))
from mhs import test_smart_ward as fixtures
SPEC = importlib.util.spec_from_file_location('mhs._admission89_candidate', Path(__file__).with_name('smart_ward.py'))
ward = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ward
SPEC.loader.exec_module(ward)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(ward.time, 'time', return_value=1000.)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.fixture = fixtures.SmartWardTests(methodName='runTest')
        with patch.object(fixtures, 'SmartWardV1', ward.SmartWardV1):
            self.fixture.setUp()
        self.engine = self.fixture.engine
        self.fixture.start()
        self.fixture.low_light()

    def tearDown(self):
        self.fixture.tearDown()

    def request(self, **changes):
        c = self.engine.model_admission_context('session-current')
        value = dict(dispatch_id=str(uuid.uuid4()), expected_run_id=c['run_id'], session_key=c['session_key'],
            expected_episode=c['episode'], expected_scene_epoch=c['scene_epoch'],
            budget_start_wall=c['budget_start_wall'], metadata_hash='a' * 64)
        value.update(changes)
        return value

    def reserve(self, **changes):
        return self.engine.reserve_model_dispatch(**self.request(**changes))

    def test_sixty_reservations_then_61st_denied_and_project_stop_requested(self):
        for n in range(1, 61):
            result = self.reserve()
            self.assertTrue(result['allowed'])
            self.assertEqual(result['ordinal'], n)
        result = self.reserve()
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reason'], 'model_dispatch_limit')
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 60)
        self.assertEqual(self.engine.s['mhs_budget']['decisions'], 0)  # Core counter retains its old meaning.
        self.assertFalse(self.engine.s['smart_ward']['watch']['enabled'])
        self.assertTrue(self.engine.s['smart_ward']['recovery_required'])
        self.assertFalse(self.engine.guard.armed)

    def test_concurrent_last_slot_has_one_winner(self):
        for _ in range(59):
            self.reserve()
        requests = [self.request(), self.request()]
        barrier = threading.Barrier(2)
        def compete(value):
            barrier.wait()
            return self.engine.reserve_model_dispatch(**value)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, requests))
        self.assertEqual(sum(r['allowed'] for r in results), 1)
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 60)

    def test_same_dispatch_id_replay_cannot_dispatch_twice(self):
        request = self.request()
        first = self.engine.reserve_model_dispatch(**request)
        second = self.engine.reserve_model_dispatch(**request)
        self.assertTrue(first['allowed'])
        self.assertFalse(second['allowed'])
        self.assertEqual(second['reason'], 'dispatch_id_already_reserved_no_second_dispatch')
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 1)
        with self.assertRaisesRegex(ValueError, 'operation_id_conflict'):
            self.engine.reserve_model_dispatch(**dict(request, metadata_hash='b' * 64))

    def test_stale_run_session_episode_scene_epoch_and_budget_are_rejected(self):
        request = self.request()
        cases = [dict(expected_run_id='other'), dict(session_key='title:random'), dict(expected_episode=2),
                 dict(expected_scene_epoch='other'), dict(budget_start_wall=request['budget_start_wall'] - 1)]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.engine.reserve_model_dispatch(**dict(request, **changes))
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 0)
        self.assertFalse(self.engine.s.get('operator_stopped'))

    def test_replay_after_stop_is_not_an_active_permission(self):
        request = self.request()
        self.engine.reserve_model_dispatch(**request)
        self.engine.stop_run('operator-test-stop', self.engine.s['run_id'])
        with self.assertRaises(ValueError):
            self.engine.reserve_model_dispatch(**request)
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 1)

    def test_idle_without_budget_is_explicitly_disabled(self):
        self.engine.s.pop('mhs_budget')
        with self.assertRaisesRegex(ValueError, 'no_active_budget_idle_chat_disabled'):
            self.engine.model_admission_context('session-current')

    def test_ended_recovery_qualification_and_paused_watch_reject(self):
        original = copy.deepcopy(self.engine.s)
        mutations = [lambda: self.engine.s['mhs_budget'].update(end_wall=1001),
            lambda: self.engine.s['smart_ward'].update(recovery_required=True),
            lambda: self.engine.s['smart_ward'].update(resume_required=True),
            lambda: self.engine.s['smart_ward'].update(fault_qualification={'active': True}),
            lambda: self.engine.s['smart_ward'].update(navigation_qualification={'active': True}),
            lambda: self.engine.s['smart_ward']['watch'].update(enabled=False)]
        for change in mutations:
            self.engine.s = copy.deepcopy(original)
            change()
            with self.assertRaises(ValueError):
                self.engine.model_admission_context('session-current')

    def test_stale_scene_pose_or_guard_reject(self):
        for name, target in [('_scene_fresh', self.engine), ('fresh', self.engine)]:
            with patch.object(target, name, return_value=False), self.assertRaisesRegex(ValueError, 'not_fresh'):
                self.engine.model_admission_context('session-current')
        with patch.object(self.engine.guard, 'state', return_value={'healthy': False}), self.assertRaisesRegex(ValueError, 'not_fresh'):
            self.engine.model_admission_context('session-current')

    def test_wall_and_sim_deadline_are_checked_in_reservation_transaction(self):
        request = self.request()
        start = self.engine.s['mhs_budget']['start_wall']
        with patch.object(ward.time, 'time', return_value=start + 3600), \
             patch.object(self.engine, '_scene_fresh', return_value=True), \
             patch.object(self.engine, 'fresh', return_value=True), \
             patch.object(self.engine.guard, 'state', return_value={'healthy': True}):
            result = self.engine.reserve_model_dispatch(**request)
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reason'], 'wall_time_limit')
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 0)

    def test_sim_deadline_and_original_core_limit_are_not_relaxed(self):
        request = self.request()
        self.engine.s['sim_time'] = self.engine.s['mhs_budget']['start_sim'] + 2400
        with patch.object(self.engine, '_scene_fresh', return_value=True):
            result = self.engine.reserve_model_dispatch(**request)
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reason'], 'sim_time_limit')

    def test_sqlite_failure_rolls_back_reservation_and_event(self):
        self.engine.db.execute("CREATE TRIGGER reject_admission BEFORE INSERT ON snapshots BEGIN SELECT RAISE(ABORT,'durable failure'); END")
        self.engine.db.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            self.reserve()
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 0)
        self.assertFalse(any(json.loads(r[0])['kind'] == 'ward_model_dispatch_reserved'
            for r in self.engine.db.execute('SELECT body FROM events WHERE run_id=?', (self.engine.s['run_id'],))))

    def test_reservation_durable_but_never_claimed_as_actual_dispatch(self):
        self.reserve()
        with closing(sqlite3.connect(self.engine.data_dir / 'state.sqlite')) as reader:
            count = reader.execute('SELECT COUNT(*) FROM ward_model_admissions').fetchone()[0]
        self.assertEqual(count, 1)
        summary = self.engine.state()['smart_ward']['model_admission']
        self.assertEqual(summary['admitted'], 1)
        self.assertIsNone(summary['actual_transport_attempts'])
        self.assertNotIn('dispatch_ids', summary)
        self.assertEqual(self.engine.verify()['provider_admission']['admitted'], 1)

    def test_old_core_meter_remains_independent(self):
        self.reserve()
        self.engine.record_model_request('core-effect', self.engine.s['run_id'], 'session-current')
        self.engine.record_model_request('core-effect', self.engine.s['run_id'], 'session-current')
        self.assertEqual(self.engine.s['mhs_budget']['decisions'], 1)
        self.assertEqual(self.engine.s['smart_ward']['model_request_ids'], ['core-effect'])
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 1)

    def test_old_core_sixty_limit_still_rejects(self):
        request = self.request()
        self.engine.s['mhs_budget']['decisions'] = 60
        result = self.engine.reserve_model_dispatch(**request)
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reason'], 'decision_limit')
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 0)

    def test_backend_restart_keeps_consumed_reservation_and_pauses_watch(self):
        request = self.request()
        self.engine.reserve_model_dispatch(**request)
        self.engine.db.close()
        self.engine = ward.SmartWardV1(self.fixture.root, self.fixture.stations, require_sim_truth=True)
        self.fixture.engine = self.engine
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 1)
        self.assertFalse(self.engine.s['smart_ward']['watch']['enabled'])
        with self.assertRaises(ValueError):
            self.engine.reserve_model_dispatch(**request)

    def test_late_previous_episode_context_cannot_consume_new_episode(self):
        request = self.request()
        self.engine.reserve_model_dispatch(**request)
        # Synthetic lifecycle fixture; real scene/Agent closure is a separate qualification.
        with self.engine._atomic():
            self.engine.s['smart_ward']['episode'] += 1
            self.engine.s['mhs_budget']['start_wall'] -= 1
        with self.assertRaisesRegex(ValueError, 'scope_or_budget_changed'):
            self.engine.reserve_model_dispatch(**dict(request, dispatch_id=str(uuid.uuid4())))
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 0)
        self.assertTrue(self.reserve()['allowed'])
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM ward_model_admissions').fetchone()[0], 2)

    def test_invalid_dispatch_and_hash_do_not_allocate(self):
        for changes in [dict(dispatch_id='-' * 36), dict(metadata_hash='z' * 64), dict(expected_episode=True), dict(budget_start_wall=True)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.reserve(**changes)
        self.assertEqual(self.engine.model_admission_summary()['admitted'], 0)


if __name__ == '__main__':
    unittest.main()
