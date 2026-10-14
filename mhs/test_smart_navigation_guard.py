"""Pure/mock tests only; no processes, Hub, API, robot or model calls."""
import argparse
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import smart_navigation as navigation
from . import smart_navigation_guard as guards
from .test_smart_navigation import complete_immediate

ROOT = Path(__file__).resolve().parents[1]


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'.cache')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def driver(self, wall=1800):
        args = argparse.Namespace(output=None, wall_seconds=wall, prior_report='unused')
        with patch.object(navigation, 'ROOT', self.root):
            return guards.GuardQualification(args)

    def prior(self):
        rows = [{'case': name, 'status': 'passed', 'pass': True} for name in navigation.ALL_CASES[:6]]
        rows += [{'case': 'change_goal_after_stop', 'status': 'failed', 'pass': False},
                 {'case': 'bridge_exit_guard', 'status': 'NA', 'pass': None},
                 {'case': 'hub_exit_guard', 'status': 'NA', 'pass': None}]
        return {'cases': rows, 'status': 'failed_or_incomplete', 'ended_wall': 1000.,
            'failure': {'case': 'change_goal_after_stop'}, 'cleanup_errors': [],
            'qualification_version': navigation.QUALIFICATION_VERSION,
            'source_sha256': {'mhs/smart_navigation.py': guards.BASE_SOURCE_SHA256},
            'final_stop': {'watch_disabled': True,
                'state': {'stopped_confirmed': True, 'pose_fresh': True, 'motion_guard': {'armed': False}},
                'scene_physical': {'available': True, 'stopped_samples': 5, 'speed_mps': 0., 'angular_speed_rad_s': 0.}}}

    def proof(self):
        return {'status': 'passed', 'cases': [{'case': name, 'status': 'passed', 'pass': True} for name in guards.REQUESTED_CASES],
            'cleanup_errors': [], 'source_unchanged': True, 'agent_model_requests_in_qualification_session': 0,
            'final_stop': {'watch_disabled': True, 'scene_physical': {'available': True, 'stopped_samples': 3,
                'speed_mps': 0., 'angular_speed_rad_s': 0.}, 'state': {'stopped_confirmed': True,
                'operator_stopped': True, 'motion_guard': {'armed': False},
                'navigation_tasks': {str(i): {} for i in range(4)}, 'orders': {},
                'smart_ward': {'specimens': {}, 'incidents': {}, 'navigation_qualification': {
                    'active': False, 'version': navigation.QUALIFICATION_VERSION, 'attempts': 4}}}}}

    def test_finite_driver_budget_and_original_backend_limits(self):
        args = guards.arguments(['--prior-report', 'old'])
        driver = self.driver(args.wall_seconds)
        self.assertEqual((driver.args.leg_seconds, driver.deadline-driver.work_end), (1200, 150))
        self.assertEqual(driver.report['limits']['maximum_driver_navigation_calls'], 4)
        self.assertEqual(driver.report['qualification_version'], 'smart-nav-20261013.3')
        self.assertEqual(driver.report['limits']['maximum_segments'], 3)
        self.assertEqual(driver.report['limits']['maximum_navigation_tasks'], 12)
        for wall in (True, 299, 1801, float('nan')):
            with self.subTest(wall=wall), self.assertRaisesRegex(ValueError, 'budget_out_of_bounds'):
                self.driver(wall)
        with patch('sys.stderr'), self.assertRaises(SystemExit):
            guards.arguments(['--prior-report', 'old', '--wall-seconds', '1801'])

    def test_exact_closed_original_is_required_and_hash_checked_first(self):
        path = self.root/guards.PRIOR_REPORT_RELATIVE
        path.parent.mkdir(parents=True)
        prior = self.prior()
        raw = json.dumps(prior).encode(); path.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        with patch.object(guards, 'PRIOR_REPORT_SHA256', sha):
            result = guards.validate_prior_report(self.root, path, 1001.)
            self.assertEqual(result['counts'], {'passed': 6, 'failed': 1, 'NA': 2})
            with self.assertRaisesRegex(ValueError, 'closed_before_this_driver'):
                guards.validate_prior_report(self.root, path, 999.)
            path.write_bytes(raw+b' ')
            with self.assertRaisesRegex(ValueError, 'sha256_mismatch'):
                guards.validate_prior_report(self.root, path, 1001.)
        with self.assertRaisesRegex(ValueError, 'exact_project'):
            guards.validate_prior_report(self.root, self.root/'other.json', 1001.)

    def test_prior_wrong_counts_source_cleanup_and_physical_stop_refused(self):
        for change in ('counts', 'source', 'cleanup', 'moving', 'active'):
            with self.subTest(change=change):
                prior = self.prior()
                if change == 'counts': prior['cases'][0]['status'] = 'NA'
                if change == 'source': prior['source_sha256']['mhs/smart_navigation.py'] = 'wrong'
                if change == 'cleanup': prior['cleanup_errors'] = ['stop_failed']
                if change == 'moving': prior['final_stop']['scene_physical']['speed_mps'] = .03
                if change == 'active': prior['final_stop']['state']['navigation'] = {'status': 'running'}
                path = self.root/guards.PRIOR_REPORT_RELATIVE
                path.parent.mkdir(parents=True, exist_ok=True)
                raw = json.dumps(prior).encode(); path.write_bytes(raw)
                with patch.object(guards, 'PRIOR_REPORT_SHA256', hashlib.sha256(raw).hexdigest()):
                    with self.assertRaisesRegex(ValueError, 'contract_failed'):
                        guards.validate_prior_report(self.root, path, 1001.)

    def test_success_requires_actual_final_stop_zero_model_and_all_four_tasks(self):
        proof = self.proof()
        self.assertTrue(guards.requested_subset_passed(proof))
        for key, value in (('cleanup_errors', ['restore_failed']), ('source_unchanged', False),
                           ('agent_model_requests_in_qualification_session', 1), ('failure', {'case': 'late_end'})):
            candidate = copy.deepcopy(proof); candidate[key] = value
            with self.subTest(key=key): self.assertFalse(guards.requested_subset_passed(candidate))
        for field, value in (('operator_stopped', False), ('stopped_confirmed', False), ('navigation_tasks', {})):
            candidate = copy.deepcopy(proof); candidate['final_stop']['state'][field] = value
            with self.subTest(field=field): self.assertFalse(guards.requested_subset_passed(candidate))
        candidate = copy.deepcopy(proof)
        candidate['final_stop']['state']['smart_ward']['navigation_qualification']['active'] = True
        self.assertFalse(guards.requested_subset_passed(candidate))

    def test_segment_labels_change_only_after_original_gate(self):
        driver = self.driver(); driver.run_id = 'run'
        original_deadline = driver.deadline
        async def inherited(index):
            driver.segment_records.append({'segment': index, 'cases': list(navigation.SEGMENTS[index-1])})
        with patch.object(navigation.Qualification, 'begin_segment', new=AsyncMock(side_effect=inherited)) as original:
            complete_immediate(driver.begin_segment(2))
        original.assert_awaited_once_with(2)
        self.assertEqual(driver.segment_records[-1]['cases'], ['bridge_exit_guard', 'hub_exit_guard'])
        self.assertEqual(driver.deadline, original_deadline)
        with patch.object(navigation.Qualification, 'begin_segment', new=AsyncMock()) as original:
            with self.assertRaisesRegex(ValueError, 'only_two_segments'):
                complete_immediate(driver.begin_segment(3))
            original.assert_not_called()

    def test_changed_original_source_refused_before_base_preflight(self):
        driver = self.driver()
        path = self.root/'mhs/smart_navigation.py'
        path.parent.mkdir(); path.write_text('changed', encoding='utf-8')
        with patch.object(navigation.Qualification, 'preflight', new=AsyncMock()) as inherited:
            with self.assertRaisesRegex(ValueError, 'frozen_source29_navigation_sha256_mismatch'):
                complete_immediate(driver.preflight())
            inherited.assert_not_called()

    def test_execute_runs_only_four_original_methods_in_two_segments(self):
        driver = self.driver()
        driver.preflight = AsyncMock()
        driver.begin_segment = AsyncMock(); driver.end_segment = AsyncMock()
        driver.hub = object()
        driver.cancel_motion = AsyncMock(return_value={'real': 'cancel'})
        driver.arrival = AsyncMock(return_value={'real': 'Lab'})
        driver.service_exit = AsyncMock(return_value={'real': 'exit'})
        async def cleanup():
            proof = self.proof()
            driver.report.update({k: v for k, v in proof.items() if k != 'cases'})
        driver.cleanup = AsyncMock(side_effect=cleanup)
        source = self.root/'sample'; source.write_bytes(b'frozen')
        driver.report['source_sha256'] = {'sample': guards.digest(source)}
        self.assertEqual(complete_immediate(driver.execute()), 2)
        driver.arrival.assert_awaited_once_with('lab')
        self.assertEqual(driver.service_exit.await_args_list[0].args, ('bridge', 'station_b'))
        self.assertEqual(driver.service_exit.await_args_list[1].args, ('daemon', 'station_c'))
        self.assertEqual(driver.begin_segment.await_count, 2)
        self.assertEqual(driver.report['status'], 'partial_NA')
        self.assertTrue(driver.report['requested_subset_passed'])
        self.assertFalse(driver.report['full_plan_acceptance_passed'])
        self.assertEqual(sum(row['status'] == 'NA' for row in driver.results), 5)

    def test_first_failure_keeps_remaining_na_and_still_cleans_up(self):
        driver = self.driver(); driver.claimed = True; driver.hub = object()
        driver.preflight = AsyncMock(); driver.begin_segment = AsyncMock()
        driver.cancel_motion = AsyncMock(side_effect=TimeoutError('real_stop_timeout'))
        driver.arrival = AsyncMock(); driver.service_exit = AsyncMock()
        driver.cleanup = AsyncMock()
        self.assertEqual(complete_immediate(driver.execute()), 1)
        driver.cleanup.assert_awaited_once()
        driver.arrival.assert_not_called(); driver.service_exit.assert_not_called()
        self.assertEqual(driver.report['failure']['case'], 'stop_real_motion')
        self.assertFalse(driver.report['requested_subset_passed'])
        self.assertEqual(sum(row['status'] == 'NA' for row in driver.results), 8)

    def test_late_actual_stop_response_cannot_pass_and_still_cleans_up(self):
        driver = self.driver(); driver.claimed = True; driver.hub = object()
        driver.work_end = 100.
        driver.preflight = AsyncMock(); driver.begin_segment = AsyncMock()
        driver.cancel_motion = AsyncMock(return_value={'late': 'actual_stop'})
        driver.arrival = AsyncMock(); driver.service_exit = AsyncMock(); driver.cleanup = AsyncMock()
        with patch.object(navigation.time, 'monotonic', return_value=100.1):
            self.assertEqual(complete_immediate(driver.execute()), 1)
        driver.cleanup.assert_awaited_once()
        driver.arrival.assert_not_called(); driver.service_exit.assert_not_called()
        self.assertFalse(any(row.get('status') == 'passed' for row in driver.results))
        self.assertIn('qualification_wall_budget_exhausted', driver.report['failure']['error'])


if __name__ == '__main__':
    unittest.main()
