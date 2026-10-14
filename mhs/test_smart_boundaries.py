"""Fail-closed driver checks; these tests do not prove simulation behavior."""
import argparse
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import smart_boundaries as boundaries


PROJECT = Path(__file__).resolve().parents[1]


class BoundaryDriverTests(unittest.TestCase):
    def setUp(self):
        (PROJECT / '.cache').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=PROJECT / '.cache')
        self.root = Path(self.temporary.name).resolve()
        self.proc = self.root / 'proc'
        self.proc.mkdir()

    def tearDown(self):
        if self.root.parent != (PROJECT / '.cache').resolve():
            raise RuntimeError('test_directory_outside_workspace_cache')
        self.temporary.cleanup()

    def driver(self, output=None):
        with patch.object(boundaries, 'ROOT', self.root), patch.dict(boundaries.os.environ, {'HOSPITAL_ROOT': str(self.root)}):
            return boundaries.Boundaries(argparse.Namespace(output=output, wall_seconds=240))

    def ledger(self):
        path = self.root / 'agent/agh-home-mhs/data/sessions.db'
        path.parent.mkdir(parents=True)
        connection = sqlite3.connect(path)
        connection.execute('CREATE TABLE events (seq INTEGER, type TEXT, data TEXT, session_key TEXT)')
        return connection

    def test_closed_workbench_is_verified_and_timeout_is_unknown(self):
        def refused(address, timeout):
            self.assertEqual(address, ('127.0.0.1', 4194))
            raise ConnectionRefusedError()
        self.assertTrue(boundaries.workbench_absent(self.root, refused, self.proc)['absent'])
        def timeout(*_, **__):
            raise TimeoutError()
        with self.assertRaisesRegex(RuntimeError, 'workbench_port_state_unknown'):
            boundaries.workbench_absent(self.root, timeout, self.proc)
        class Listening:
            def close(self): pass
        self.assertFalse(boundaries.workbench_absent(self.root, lambda *a, **k: Listening(), self.proc)['absent'])

    def test_recorded_live_pid_with_different_ownership_is_refused(self):
        marker = self.root / 'logs/mhs/workbench.pid'
        marker.parent.mkdir(parents=True)
        marker.write_text('123')
        directory = self.proc / '123'
        directory.mkdir()
        (directory / 'stat').write_text('123 (node) S other fields')
        (directory / 'cmdline').write_bytes(b'node\0unrelated-job\0')
        (directory / 'cwd').mkdir()
        with self.assertRaisesRegex(RuntimeError, 'process_ownership_mismatch'):
            boundaries.workbench_absent(self.root, lambda *a, **k: None, self.proc)

    def test_model_audit_is_scoped_and_detects_pending_inference(self):
        connection = self.ledger()
        connection.executemany('INSERT INTO events VALUES (?,?,?,?)',
            [(1, 'request/sent', '{}', 'other-session'),
             (2, 'session/start', '{}', 'selected'),
             (3, 'effect/intent', json.dumps({'kind': 'inference'}), 'selected')])
        connection.commit()
        connection.close()
        audit = boundaries.selected_session_model_audit(self.root, 'selected')
        self.assertEqual(audit['request_sent_count'], 0)
        self.assertEqual(audit['inference_intent_count'], 1)
        driver = self.driver()
        driver.session = 'selected'
        with self.assertRaisesRegex(RuntimeError, 'unexpected_model_request'):
            driver.audit_zero_model({})

    def test_failed_refusal_invariant_detects_any_specimen_or_order_change(self):
        driver = self.driver()
        before = {'orders': {'O': {'loaded': False, 'status': 'created', 'package_id': 'S'}},
                  'smart_ward': {'specimens': {'S': {'location': 'hospital-cabinet-b'}}},
                  'navigation_tasks': {}}
        after = copy.deepcopy(before)
        after['smart_ward']['specimens']['S']['location'] = 'robot'
        with patch.object(driver, 'state', side_effect=[before, after]):
            with self.assertRaisesRegex(AssertionError, 'refused_operation_changed'):
                driver.reject_unchanged(lambda: {'error': 'rejected'})
        after = copy.deepcopy(before)
        after['orders']['O']['loaded'] = True
        with patch.object(driver, 'state', side_effect=[before, after]):
            with self.assertRaisesRegex(AssertionError, 'refused_operation_changed'):
                driver.reject_unchanged(lambda: {'error': 'rejected'})

    def test_evidence_path_cannot_escape_acceptance_or_overwrite_attempt(self):
        with self.assertRaisesRegex(ValueError, 'evidence_must_be_new_child'):
            self.driver(str(self.root / 'outside'))
        path = self.root / 'runs/smart-ward-v1/acceptance/fixed-attempt'
        self.driver(str(path))
        with self.assertRaises(FileExistsError):
            self.driver(str(path))

    def test_cleanup_never_stops_a_different_run(self):
        driver = self.driver()
        driver.start_attempted = True
        driver.run_id = 'original'
        with patch.object(driver, 'api', return_value={'run_id': 'different'}) as api:
            with self.assertRaisesRegex(RuntimeError, 'refuses_to_stop_changed_run'):
                driver.stop()
        self.assertEqual(api.call_count, 1)

    def sampled_scene(self, sequence, specimens=('S1', 'S2', 'S3'), wall=999., sim=20.):
        return {'run_id': 'run', 'epoch': 'epoch', 'seq': sequence,
                'wall_time': wall, 'sim_time': sim,
                'specimens': {sid: {'location': 'hospital-cabinet-b'} for sid in specimens}}

    def sampled_state(self, scene, incidents=('S1', 'S2', 'S3'), orders=None):
        return {'run_id': 'run', 'orders': orders or {}, 'smart_ward': {
            'scene_sample': {key: scene.get(key) for key in ('run_id', 'epoch', 'seq', 'wall_time', 'sim_time')}, 'scene_fresh': True,
            'specimens': copy.deepcopy(scene['specimens']),
            'incidents': [{'incident_id': 'I-' + str(index), 'specimen_id': sid}
                          for index, sid in enumerate(incidents)]}}

    def sampled_driver(self):
        driver = self.driver()
        driver.run_id, driver.epoch = 'run', 'epoch'
        driver.specimens = {'b1': 'S1', 'b2': 'S2', 'c1': 'S3'}
        return driver

    def test_sampling_consumes_real_engine_public_contract_without_private_scene(self):
        from .test_smart_ward import SmartWardTests
        fixture = SmartWardTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        for sid in ('S1', 'S2', 'S3'):
            fixture.specimens[sid] = {'specimen_id': sid, 'location': 'hospital-cabinet-b', 'sealed': True}
        fixture.observe()
        public = fixture.engine.state()
        actual = json.loads((fixture.engine.scene_dir / 'observation.json').read_text())
        self.assertNotIn('scene', public['smart_ward'])
        self.assertEqual(public['smart_ward']['scene_sample'],
            {key: actual[key] for key in ('run_id', 'epoch', 'seq', 'wall_time', 'sim_time')})
        self.assertNotIn('specimens', public['smart_ward']['scene_sample'])
        driver = self.sampled_driver()
        driver.run_id, driver.epoch = public['run_id'], public['smart_ward']['epoch']
        with patch.object(driver, 'guard'), patch.object(driver, 'api', return_value=public), \
             patch.object(driver, 'observation', return_value=actual), patch.object(boundaries, 'physical_stationary', return_value=True):
            state, observed, proof = driver.wait_sampling('public-contract',
                after_ack={'wall_time': actual['wall_time'] - .01, 'sim_time': actual['sim_time']})
        self.assertEqual(proof['backend_sequence'], actual['seq'])
        self.assertEqual(set(state['smart_ward']['specimens']), {'S1', 'S2', 'S3'})
        self.assertEqual(observed, actual)

    def test_capacity_waits_for_original_ack_then_backend_sampling_without_reinsertion(self):
        driver = self.sampled_driver()
        driver.specimens = {}
        outcomes = []
        for label, sid in [('b1', 'S1'), ('b2', 'S2'), ('c1', 'S3'), ('b3-full', None)]:
            operation = driver.prefix + '-' + label
            status = 'completed' if sid else 'failed'
            ack = {'run_id': 'run', 'epoch': 'epoch', 'operation_id': operation,
                   'status': status, 'result': {'specimen_id': sid} if sid else {},
                   'wall_time': 998., 'sim_time': 20.}
            result = {'operation_id': operation, 'status': status, 'result': ack['result'], 'ack': ack}
            if not sid:
                result['error'] = 'source_cabinet_full'
            outcomes.append(({'operation_id': operation}, {'operation_id': operation}, result))
        before_ack = self.sampled_scene(8, ('S1', 'S2'), wall=997.)
        backend_lag = self.sampled_scene(9, ('S1', 'S2'), wall=999.)
        aligned = self.sampled_scene(10)
        with patch.object(driver, 'scene_command', side_effect=outcomes) as commands, \
             patch.object(driver, 'api', return_value={'operation_id': outcomes[0][0]['operation_id'], 'result': {'specimen_id': 'S1'}}) as replay, \
             patch.object(driver, 'state', side_effect=[self.sampled_state(before_ack), self.sampled_state(backend_lag), self.sampled_state(aligned)]) as states, \
             patch.object(driver, 'observation', side_effect=[before_ack, self.sampled_scene(10), self.sampled_scene(11)]), \
             patch.object(boundaries.time, 'time', return_value=1000.), patch.object(boundaries.time, 'sleep'):
            result = driver.source_capacity_and_operation_replay()
        self.assertEqual(commands.call_count, 4)
        self.assertEqual(replay.call_count, 1)  # Only the originally specified operation replay.
        self.assertEqual(states.call_count, 3)
        self.assertEqual(result['sampling']['required_sequence'], 10)
        self.assertEqual(result['sampling']['backend_sequence'], 10)
        self.assertEqual(len(result['insertion_acks']), 4)

    def test_sampling_extra_specimen_or_order_fails_immediately_and_preserves_evidence(self):
        for kind in ('backend_specimen', 'observed_specimen', 'order'):
            with self.subTest(kind=kind):
                driver = self.sampled_driver()
                old = driver.directory / 'failed-physical-observation.json'
                old.write_text('original failure must remain unchanged')
                scene = self.sampled_scene(10)
                observed = copy.deepcopy(scene)
                state = self.sampled_state(scene)
                if kind == 'backend_specimen':
                    state['smart_ward']['specimens']['EXTRA'] = {}
                elif kind == 'observed_specimen':
                    observed['specimens']['EXTRA'] = {}
                else:
                    state['orders']['EXTRA-ORDER'] = {}
                with patch.object(driver, 'state', return_value=state) as states, \
                     patch.object(driver, 'observation', return_value=observed), \
                     patch.object(boundaries.time, 'time', return_value=1000.), patch.object(boundaries.time, 'sleep') as sleep:
                    with self.assertRaisesRegex(AssertionError, 'extra_specimen|unexpected_order'):
                        driver.wait_sampling('capacity', after_ack={'wall_time': 998., 'sim_time': 20.})
                self.assertEqual(states.call_count, 1)
                sleep.assert_not_called()
                artifacts = list(driver.directory.glob('failed-sampling-*.json'))
                self.assertEqual(len(artifacts), 1)
                evidence = json.loads(artifacts[0].read_text())
                self.assertEqual(evidence['state'], state)
                self.assertEqual(evidence['observation'], observed)
                self.assertEqual(old.read_text(), 'original failure must remain unchanged')

    def test_incident_dedup_waits_for_backend_third_new_sample_and_missing_incident(self):
        driver = self.sampled_driver()
        initial = self.sampled_scene(10)
        lagging = self.sampled_scene(12)
        aligned = self.sampled_scene(13)
        with patch.object(driver, 'state', side_effect=[self.sampled_state(initial),
                self.sampled_state(lagging, incidents=('S1', 'S2')),
                self.sampled_state(aligned, incidents=('S1', 'S2')), self.sampled_state(aligned)]) as states, \
             patch.object(driver, 'observation', side_effect=[initial, self.sampled_scene(13), self.sampled_scene(14), self.sampled_scene(15)]), \
             patch.object(boundaries.time, 'time', return_value=1000.), patch.object(boundaries.time, 'sleep'):
            result = driver.fresh_sampling_incident_deduplication()
        self.assertEqual(states.call_count, 4)
        self.assertEqual(result['sampling']['required_sequence'], 13)
        self.assertEqual(result['sampling']['backend_sequence'], 13)
        self.assertEqual(len(result['incidents']), 3)

    def test_duplicate_or_foreign_incident_fails_before_waiting_for_sample_barrier(self):
        for incidents in (('S1', 'S1'), ('S1', 'FOREIGN')):
            with self.subTest(incidents=incidents):
                driver = self.sampled_driver()
                scene = self.sampled_scene(10)
                with patch.object(driver, 'state', return_value=self.sampled_state(scene, incidents=incidents)), \
                     patch.object(driver, 'observation', return_value=scene), \
                     patch.object(boundaries.time, 'time', return_value=1000.), patch.object(boundaries.time, 'sleep') as sleep:
                    with self.assertRaisesRegex(AssertionError, 'duplicated_or_unexpected_incident'):
                        driver.wait_sampling('dedup', minimum_sequence=13, require_incidents=True)
                sleep.assert_not_called()

    def test_sampling_timeout_uses_existing_work_budget_and_saves_last_pair(self):
        driver = self.sampled_driver()
        driver.work_deadline = .4
        clock = [0.]
        scene = self.sampled_scene(10, specimens=('S1', 'S2'))
        state = self.sampled_state(scene, incidents=('S1', 'S2'))
        def advance(seconds):
            clock[0] += seconds
        with patch.object(driver, 'state', return_value=state) as states, \
             patch.object(driver, 'observation', return_value=scene), \
             patch.object(boundaries.time, 'time', return_value=1000.), \
             patch.object(boundaries.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(boundaries.time, 'sleep', side_effect=advance):
            with self.assertRaisesRegex(TimeoutError, 'sampling_not_synchronized'):
                driver.wait_sampling('capacity', after_ack={'wall_time': 998., 'sim_time': 20.})
        self.assertAlmostEqual(clock[0], .4)
        self.assertEqual(states.call_count, 1)
        evidence = json.loads(next(driver.directory.glob('failed-sampling-*.json')).read_text())
        self.assertEqual(evidence['state'], state)
        self.assertEqual(evidence['observation'], scene)

    def test_sampling_wrong_run_is_refused_and_never_accepted_as_a_new_sample(self):
        driver = self.sampled_driver()
        scene = self.sampled_scene(10)
        scene['run_id'] = 'different'
        with patch.object(driver, 'state', return_value=self.sampled_state(scene)), \
             patch.object(driver, 'observation', return_value=scene), patch.object(boundaries.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'sampling_scene_identity_changed'):
                driver.wait_sampling('capacity', after_ack={'wall_time': 998., 'sim_time': 20.})
        sleep.assert_not_called()

    def test_sampling_does_not_retry_or_relax_stationary_guard_failure(self):
        driver = self.sampled_driver()
        with patch.object(driver, 'state', side_effect=RuntimeError('stationary_qualification_requires_continuous_fresh_physical_stop')) as states, \
             patch.object(driver, 'observation') as observation, patch.object(boundaries.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'continuous_fresh_physical_stop'):
                driver.wait_sampling('capacity', after_ack={'wall_time': 998., 'sim_time': 20.})
        self.assertEqual(states.call_count, 1)
        observation.assert_not_called()
        sleep.assert_not_called()

    def test_case_failure_leaves_remaining_cases_na_and_requires_cleanup(self):
        driver = self.driver()
        with patch.object(driver, 'preflight'), patch.object(driver, 'source_capacity_and_operation_replay', side_effect=AssertionError('real failure')), \
             patch.object(driver, 'stop', return_value={'physical_stop_confirmed': True}) as stop:
            source = self.root / 'mhs'
            source.mkdir()
            for name in ('smart_ward.py', 'smart_scene.py'):
                (source / name).write_text('# fixture')
            # The actual driver file is hashed; make the temporary project match
            # that path relation while retaining the real __file__.
            with patch.object(boundaries.hashlib, 'sha256') as digest:
                digest.return_value.hexdigest.return_value = 'test-only'
                with patch.object(boundaries.Path, 'relative_to', return_value=Path('test-only')):
                    code = driver.run()
        self.assertEqual(code, 1)
        self.assertEqual(stop.call_count, 1)
        self.assertEqual(driver.results[boundaries.CASES[0]]['status'], 'failed')
        self.assertTrue(all(driver.results[name]['status'] == 'NA' for name in boundaries.CASES[1:]))
        self.assertFalse(driver.report['full_plan_acceptance_passed'])


if __name__ == '__main__':
    unittest.main()
