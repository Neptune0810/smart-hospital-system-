"""Navigation qualification budget tests; no Hub, ROS, simulator or SSH."""
import argparse
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import smart_navigation as navigation


ROOT = Path(__file__).resolve().parents[1]


def complete_immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('test_fixture_unexpectedly_requires_async_io')


class NavigationBudgetTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.cache').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT/'.cache')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def driver(self, wall=7200, leg=1200):
        with patch.object(navigation, 'ROOT', self.root):
            driver = navigation.Qualification(argparse.Namespace(output=None, wall_seconds=wall, leg_seconds=leg))
        driver.run_id, driver.epoch, driver.session = 'run', 'epoch', 'qualification:session'
        return driver

    def qualification(self):
        return {'version': navigation.QUALIFICATION_VERSION, 'active': True,
                'session_key': 'qualification:session', 'segment': 1, 'started_wall': 1000.,
                'segment_started_wall': 1000., 'deadline_wall': 3400.,
                'total_deadline_wall': 8200., 'max_attempts': 12}

    def test_defaults_revision_and_finite_cli_limits(self):
        args = navigation.arguments([])
        self.assertEqual((args.wall_seconds, args.leg_seconds), (7200, 1200))
        with patch('sys.stderr'):
            for arguments in (['--wall-seconds', '7201'], ['--leg-seconds', '1201'], ['--leg-seconds', 'nan']):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    navigation.arguments(arguments)
        for wall, leg in ((7201, 1200), (7200, 1201), (float('nan'), 1200), (7200, True)):
            with self.subTest(wall=wall, leg=leg), self.assertRaisesRegex(ValueError, 'out_of_bounds'):
                self.driver(wall, leg)
        driver = self.driver()
        report = driver.report
        self.assertEqual(report['qualification_version'], 'smart-nav-20261013.3')
        self.assertEqual(report['limits']['segment_wall_seconds'], 2400)
        self.assertEqual(report['limits']['maximum_segments'], 3)
        self.assertEqual(report['limits']['maximum_navigation_tasks'], 12)
        self.assertEqual(report['budget_revision']['prior_limits'],
                         {'leg_wall_seconds': 900, 'segment_wall_seconds': 1800, 'global_wall_seconds': 5400})
        self.assertEqual(report['budget_revision']['source25_b_elapsed_wall_seconds_approx'], 753)
        self.assertAlmostEqual(report['budget_revision']['source25_lab_observation']
                               ['estimated_total_lower_bound_wall_seconds'], 954.8515715, places=4)
        self.assertEqual(driver.deadline-driver.work_end, 150)

    def test_segment_uses_new_version_without_resetting_driver_global_deadline(self):
        driver = self.driver()
        original_deadline = driver.deadline
        driver.state = AsyncMock(return_value=({}, {}))
        driver.api = AsyncMock(return_value={'qualification': self.qualification()})
        with patch.object(navigation, 'physical_stationary', return_value=True), \
             patch.object(navigation.time, 'time', return_value=1000.), \
             patch.object(navigation.time, 'monotonic', return_value=100.):
            complete_immediate(driver.begin_segment(1))
        self.assertEqual(driver.segment_end, 2500.)
        self.assertEqual(driver.deadline, original_deadline)

    def test_old_version_and_nonfinite_or_extended_metadata_are_refused(self):
        changes = ({'version': None}, {'version': 'smart-nav-20261013.1'},
                   {'version': 'smart-nav-20261013.2'},
                   {'deadline_wall': float('nan')}, {'deadline_wall': 3401.},
                   {'total_deadline_wall': 8201.}, {'max_attempts': 13})
        for change in changes:
            with self.subTest(change=change):
                driver = self.driver()
                q = dict(self.qualification(), **change)
                driver.state = AsyncMock(return_value=({}, {}))
                driver.api = AsyncMock(return_value={'qualification': q})
                with patch.object(navigation, 'physical_stationary', return_value=True), \
                     patch.object(navigation.time, 'time', return_value=1000.):
                    with self.assertRaisesRegex(RuntimeError, 'version_requires_new_run|budget_metadata_missing'):
                        complete_immediate(driver.begin_segment(1))
                self.assertTrue(driver.claimed)  # A remote accepted begin must still be stopped.
                self.assertIsNone(driver.segment_end)

    def test_leg_times_out_at_1200_without_relaxing_arrival_or_qualification_limits(self):
        driver = self.driver()
        clock = [1199.]
        driver.work_end, driver.segment_end = 7050., 2400.
        driver.hub = type('Hub', (), {'jobs': {}})()
        driver.state = AsyncMock(return_value=({'navigation_tasks': {'T': {'status': 'running'}}}, {}))
        def advance(seconds):
            clock[0] += 2
        task = {'task_id': 'T', 'station': 'station_b', 'started_monotonic': 0., 'receipt': {'job': 'J'}}
        with patch.object(navigation.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(navigation.asyncio, 'sleep', new=AsyncMock(side_effect=advance)), \
             patch.object(navigation, 'arrived', return_value=False) as arrived:
            with self.assertRaisesRegex(TimeoutError, 'real_arrival_leg_or_qualification_budget_exhausted'):
                complete_immediate(driver.complete_route(task))
        self.assertEqual(driver.state.call_count, 1)
        arrived.assert_called_once()

    def test_response_crossing_any_earliest_deadline_cannot_pass_arrival(self):
        # A real state request may start inside the budget and return after it.
        # All three independent cutoffs must reject that late success response.
        for owner, work_end, segment_end, cutoff in (
                ('leg', 7050., 2400., 1200.),
                ('segment', 7050., 500., 500.),
                ('global_work', 700., 2400., 700.)):
            with self.subTest(owner=owner):
                driver = self.driver()
                driver.work_end, driver.segment_end = work_end, segment_end
                driver.hub = type('Hub', (), {'jobs': {'J': {'status': 'done'}}})()
                clock = [cutoff-.1]
                def late_state():
                    clock[0] = cutoff+.1
                    return {'navigation_tasks': {'T': {'status': 'succeeded'}}}, {}
                driver.state = AsyncMock(side_effect=late_state)
                task = {'task_id': 'T', 'station': 'lab', 'started_monotonic': 0.,
                        'receipt': {'job': 'J'}}
                with patch.object(navigation.time, 'monotonic', side_effect=lambda: clock[0]), \
                     patch.object(navigation, 'arrived', return_value=True) as arrived:
                    with self.assertRaisesRegex(TimeoutError, 'real_arrival_leg_or_qualification_budget_exhausted'):
                        complete_immediate(driver.complete_route(task))
                driver.state.assert_awaited_once()
                arrived.assert_not_called()

    def test_success_before_earliest_deadline_requires_actual_arrival_and_native_done(self):
        driver = self.driver()
        driver.work_end, driver.segment_end = 7050., 600.
        driver.hub = type('Hub', (), {'jobs': {'J': {'status': 'done'}}})()
        driver.state = AsyncMock(return_value=({'navigation_tasks': {'T': {'status': 'succeeded'}}},
                                               {'physical_robot': {'available': True}}))
        task = {'task_id': 'T', 'station': 'lab', 'started_monotonic': 0.,
                'receipt': {'job': 'J'}}
        with patch.object(navigation.time, 'monotonic', return_value=599.), \
             patch.object(navigation, 'arrived', return_value=True) as arrived:
            result = complete_immediate(driver.complete_route(task))
        self.assertEqual(result['hub_result']['status'], 'done')
        arrived.assert_called_once_with(result['state'], {'physical_robot': {'available': True}},
                                        'lab', 'T', 'run', 'epoch')

    def test_deadline_revision_preserves_failed_canceled_unknown_and_native_failure(self):
        task = {'task_id': 'T', 'station': 'lab', 'started_monotonic': 0.,
                'receipt': {'job': 'J'}}
        for status in ('failed', 'canceled', 'unknown', 'running'):
            with self.subTest(status=status):
                driver = self.driver()
                driver.work_end, driver.segment_end = 7050., 2400.
                driver.hub = type('Hub', (), {'jobs': {'J': {'status': 'failed'}}})()
                driver.state = AsyncMock(return_value=({'navigation_tasks': {'T': {'status': status}}}, {}))
                reason = 'hub_navigation_failed' if status == 'running' else 'real_navigation_'+status
                with patch.object(navigation.time, 'monotonic', return_value=100.), \
                     patch.object(navigation, 'arrived', return_value=False):
                    with self.assertRaisesRegex(RuntimeError, reason):
                        complete_immediate(driver.complete_route(task))

    def test_remaining_clips_segment_and_global_work_but_reserves_cleanup(self):
        driver = self.driver()
        driver.deadline, driver.work_end, driver.segment_end = 7200., 7050., 2400.
        with patch.object(navigation.time, 'monotonic', return_value=2399.):
            self.assertEqual(driver.remaining(), 1)
            self.assertEqual(driver.remaining(cleanup=True), 4801)
        with patch.object(navigation.time, 'monotonic', return_value=2400.):
            with self.assertRaisesRegex(TimeoutError, 'qualification_wall_budget_exhausted'):
                driver.remaining()
        driver.segment_end = 9000.
        with patch.object(navigation.time, 'monotonic', return_value=7049.):
            self.assertEqual(driver.remaining(), 1)
        with patch.object(navigation.time, 'monotonic', return_value=7050.):
            with self.assertRaisesRegex(TimeoutError, 'qualification_wall_budget_exhausted'):
                driver.remaining()
            self.assertEqual(driver.remaining(cleanup=True), 150)
        with patch.object(navigation.time, 'monotonic', return_value=7200.):
            with self.assertRaisesRegex(TimeoutError, 'qualification_wall_budget_exhausted'):
                driver.remaining(cleanup=True)

    def test_leg_timeout_execute_still_requests_real_stop_and_preserves_later_na(self):
        driver = self.driver()
        driver.claimed = True
        driver.preflight = AsyncMock()
        driver.begin_segment = AsyncMock()
        driver.connect_hub = AsyncMock()
        driver.arrival = AsyncMock(side_effect=TimeoutError('real_arrival_leg_or_qualification_budget_exhausted:station_b'))
        driver.api = AsyncMock(return_value={})
        driver.wait_stopped = AsyncMock(return_value=({'smart_ward': {'watch': {'enabled': False}}}, {'physical_robot': {}}))
        code = complete_immediate(driver.execute())
        self.assertEqual(code, 1)
        paths = [call.args[0] for call in driver.api.call_args_list]
        self.assertIn('/api/admin/smart-ward/navigation-qualification/end', paths)
        self.assertIn('/api/admin/stop-run', paths)
        stop = next(call.args[1] for call in driver.api.call_args_list if call.args[0] == '/api/admin/stop-run')
        self.assertEqual(stop['expected_run_id'], 'run')
        self.assertEqual(driver.report['status'], 'failed_or_incomplete')
        self.assertTrue(all(row['status'] == 'NA' for row in driver.results if row['case'] != 'arrival_b'))


if __name__ == '__main__':
    unittest.main()
