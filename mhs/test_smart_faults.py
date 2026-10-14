"""Fault driver safety checks; no simulator, model, Hub or SSH is started."""
import argparse
import copy
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from . import smart_boundaries, smart_faults


PROJECT = Path(__file__).resolve().parents[1]


def complete_immediate(coroutine):
    """These AsyncMocks finish inline; no socket-pair/event loop is needed."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('test_fixture_unexpectedly_requires_real_async_io')


class FaultDriverTests(unittest.TestCase):
    def setUp(self):
        (PROJECT / '.cache').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=PROJECT / '.cache')
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        if self.root.parent != (PROJECT / '.cache').resolve():
            raise RuntimeError('test_directory_outside_workspace_cache')
        self.temporary.cleanup()

    def driver(self, case='stationary'):
        output = str(self.root / 'runs/smart-ward-v1/acceptance' / ('test-' + case + '-' + str(time.time_ns())))
        with patch.object(smart_boundaries, 'ROOT', self.root), patch.dict(smart_boundaries.os.environ, {'HOSPITAL_ROOT': str(self.root)}):
            return smart_faults.Faults(argparse.Namespace(case=case, wall_seconds=240 if case == 'stationary' else 1800, output=output))

    def pause_state(self):
        observation = {'seq': 20, 'sim_time': 5, 'wall_time': time.time() - 5}
        state = {'smart_ward': {'scene_fresh': False, 'incidents': [{'incident_id': 'I', 'status': 'new'}],
            'devices': {ident: {'fresh': False, 'available': False} for ident in smart_faults.DEVICES}}}
        return state, observation

    def test_stale_pause_requires_unchanged_time_sequence_and_explicit_unknown(self):
        state, observation = self.pause_state()
        self.assertTrue(smart_faults.stale_pause_evidence(state, observation, 'I', copy.deepcopy(observation)))
        for key in ('seq', 'sim_time', 'wall_time'):
            changed = copy.deepcopy(observation)
            changed[key] += 1
            self.assertFalse(smart_faults.stale_pause_evidence(state, changed, 'I', observation))
        for mutation in ('fresh', 'resolved', 'available'):
            changed = copy.deepcopy(state)
            if mutation == 'fresh': changed['smart_ward']['scene_fresh'] = True
            elif mutation == 'resolved': changed['smart_ward']['incidents'][0]['status'] = 'resolved'
            else: changed['smart_ward']['devices']['hospital-sensor-301']['available'] = True
            self.assertFalse(smart_faults.stale_pause_evidence(changed, observation, 'I', observation))

    def test_mutation_refuses_unexpected_workbench_even_during_cleanup(self):
        driver = self.driver()
        with patch.object(smart_faults, 'workbench_absent', return_value={'absent': False}), patch.object(driver, 'raw_api') as raw:
            with self.assertRaisesRegex(RuntimeError, 'cleanup_mutation_requires'):
                driver.api('/api/admin/smart-ward/fault-qualification/end', {}, cleanup=True)
        raw.assert_not_called()

    def test_expired_qualification_cannot_extend_itself(self):
        driver = self.driver()
        driver.session = 'qualification:test'
        driver.qualification = {'deadline_wall': time.time() - 1}
        with patch.object(smart_faults, 'workbench_absent', return_value={'absent': True}), patch.object(driver, 'audit_zero_model') as audit:
            with self.assertRaisesRegex(TimeoutError, 'qualification_expired'):
                driver.guard()
        audit.assert_called_once()

    def test_failed_native_call_must_match_exact_expected_reason(self):
        driver = self.driver()
        state = {'orders': {}, 'smart_ward': {'specimens': {}, 'transfers': {}}, 'navigation_tasks': {}}
        driver.astate = AsyncMock(return_value=state)
        driver.rpc = AsyncMock(return_value={'accepted': True, 'job': 'J'})
        driver.finish = AsyncMock(return_value={'status': 'failed', 'error': {'message': 'network disconnected'}})
        with self.assertRaisesRegex(AssertionError, 'reason_not_proved'):
            complete_immediate(driver.native_refusal('hospital-cabinet-b', 'handoff_order', {}, 'device_unavailable_or_stale'))
        driver.finish = AsyncMock(return_value={'status': 'failed', 'error': {'message': 'device_unavailable_or_stale'}})
        result = complete_immediate(driver.native_refusal('hospital-cabinet-b', 'handoff_order', {}, 'device_unavailable_or_stale'))
        self.assertEqual(result['expected_reason'], 'device_unavailable_or_stale')

    def test_ack_race_does_not_retry_or_arm_fault_after_completed_handoff(self):
        driver = self.driver('ack')
        driver.order_id = 'O-real'
        driver.create_specimen_order = AsyncMock(return_value={})
        driver.arrival_b = AsyncMock(return_value={})
        driver.device_state = AsyncMock(return_value={})
        driver.rpc = AsyncMock(return_value={'accepted': True, 'job': 'one-handoff'})
        driver.astate = AsyncMock(side_effect=lambda **kwargs: {'smart_ward': {'operations': {
            driver.prefix + '-pickup-original': {'status': 'completed'}}}})
        driver.fault = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, 'missed_pending_handoff_window_no_retry'):
            complete_immediate(driver.ack())
        self.assertEqual(driver.rpc.call_count, 1)
        driver.fault.assert_not_called()
        self.assertEqual(driver.handoff_operation, driver.prefix + '-pickup-original')

    def prepared_arrival(self):
        """Real arrival/stop predicate over interface fixtures, not real motion."""
        driver = self.driver('ack')
        driver.run_id, driver.epoch = 'run', 'epoch'
        task = {'route_id': 'R', 'goal_station': 'station_b', 'status': 'succeeded'}
        now = time.time()
        state = {'navigation_tasks': {'N': task}, 'navigation': task,
            'stations': {'station_b': {'x': 0, 'y': 0}}, 'at_stations': ['station_b'],
            'pose_fresh': True, 'pose_wall_time': now, 'stopped_confirmed': True,
            'pose': {'speed': 0, 'angular_speed': 0},
            'physical_pose': {'position': [0, 0], 'wall_time': now}}
        observation = {'run_id': 'run', 'epoch': 'epoch', 'seq': 5, 'wall_time': now,
            'physical_robot': {'wall_time': now, 'available': True, 'stopped_samples': 3,
                               'speed_mps': 0, 'angular_speed_rad_s': 0}}
        driver.hub = SimpleNamespace(jobs={'J': {'status': 'done'}})
        driver.publish_plan = AsyncMock(return_value={})
        driver.aapi = AsyncMock(return_value={'routes': [
            {'route_id': 'R', 'validation': 'nav2_verified', 'distance_m': 10}]})
        driver.device_state = AsyncMock(return_value={})
        driver.rpc = AsyncMock(return_value={'accepted': True, 'job': 'J'})
        driver.astate = AsyncMock(return_value=state)
        driver.observation = lambda: observation
        return driver

    def test_verified_arrival_after_prior_600_limit_keeps_original_total_budget(self):
        driver = self.prepared_arrival()
        original_deadline, original_work = driver.deadline, driver.work_deadline
        with patch.object(smart_faults.time, 'monotonic', side_effect=[0, 772]):
            result = complete_immediate(driver.arrival_b())
        self.assertEqual(result['navigation']['task_id'], 'N')
        self.assertEqual(driver.deadline, original_deadline)
        self.assertEqual(driver.work_deadline, original_work)
        self.assertAlmostEqual(original_deadline - driver.started, 1800)
        self.assertAlmostEqual(original_deadline - original_work, 60)
        self.assertEqual(driver.report['qualification_version'], 'smart-fault-ack-20261013.2')
        self.assertEqual(driver.report['limits']['navigation_leg_wall_seconds'], 900)
        revision = driver.report['budget_revision']
        self.assertEqual(revision['original_navigation_evidence_id'], '072950-944c9c29')
        self.assertEqual(revision['reference_navigation_qualification_version'], 'smart-nav-20261013.2')
        driver.rpc.assert_awaited_once()

    def test_navigation_leg_still_times_out_and_does_not_extend_global_work_deadline(self):
        for work_deadline, observed in ((1740, 901), (700, 772)):
            with self.subTest(work_deadline=work_deadline):
                # Separate drivers use separate output directories; neither
                # timed-out leg is retried or receives a fresh deadline.
                driver = self.prepared_arrival()
                driver.work_deadline = work_deadline
                with patch.object(smart_faults.time, 'monotonic', side_effect=[0, observed]):
                    with self.assertRaisesRegex(TimeoutError, 'within_900_wall_seconds'):
                        complete_immediate(driver.arrival_b())
                self.assertEqual(driver.work_deadline, work_deadline)
                driver.astate.assert_not_awaited()
                driver.rpc.assert_awaited_once()

    def test_cleanup_refuses_a_different_run_before_any_mutation(self):
        driver = self.driver()
        driver.start_attempted = True
        driver.run_id = 'original'
        driver.aapi = AsyncMock(return_value={'run_id': 'different'})
        with self.assertRaisesRegex(RuntimeError, 'refuses_to_mutate_changed_run'):
            complete_immediate(driver.cleanup())
        self.assertEqual(driver.aapi.call_count, 1)

    def test_stationary_scope_rejects_external_navigation(self):
        driver = self.driver()
        self.assertEqual(driver.report['limits']['navigation_leg_wall_seconds'], 0)
        self.assertEqual(driver.report['limits']['maximum_navigation_tasks'], 0)
        with patch.object(driver, 'guard'), patch.object(driver, 'api', return_value={
            'orders': {}, 'smart_ward': {}, 'navigation_tasks': {'unexpected': {'status': 'running'}}}):
            with self.assertRaisesRegex(RuntimeError, 'unexpected_navigation_scope'):
                driver.state()


if __name__ == '__main__':
    unittest.main()
