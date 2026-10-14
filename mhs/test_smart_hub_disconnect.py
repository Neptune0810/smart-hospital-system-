"""Hub-only qualification contract tests: pure/mocked, never live execution."""
import argparse
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import smart_navigation as navigation
from . import smart_disconnect as disconnect
from . import smart_hub_disconnect as hub
from . import test_smart_disconnect as prior_tests
from .test_smart_navigation import complete_immediate

ROOT = Path(__file__).resolve().parents[1]


class HubDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'.cache')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = prior_tests.DisconnectTests()

    def driver(self, wall=600):
        with patch.object(navigation, 'ROOT', self.root):
            driver = hub.HubDisconnectQualification(argparse.Namespace(
                output=None, prior_report='unused', wall_seconds=wall))
        driver.run_id = 'run'; driver.started_wall = 100.
        return driver

    def proof(self):
        proof = self.fixture.proof()
        proof['cases'] = [{'case': hub.CASE, 'status': 'passed', 'pass': True}]
        state = proof['final_stop']['state']
        state['navigation_tasks'].pop('2')
        state['smart_ward']['navigation_qualification']['attempts'] = 1
        return proof

    def prior(self):
        proof = self.fixture.prior()
        proof['cases'] = [{'case': name, 'status': 'passed' if name == 'bridge_exit_guard' else 'failed' if name == 'hub_exit_guard' else 'NA',
            'pass': True if name == 'bridge_exit_guard' else False if name == 'hub_exit_guard' else None} for name in navigation.ALL_CASES]
        proof['failure']['case'] = hub.CASE
        proof['qualification_scope_version'] = disconnect.QUALIFICATION_SCOPE_VERSION
        proof['source_sha256'].update(hub.SOURCE43_FILES)
        proof['stop_record_proof'] = disconnect.stop_record_proof(proof['final_stop']['state'], 'run', self.fixture.submitted(), 100.)
        proof['prior_report']['sha256'] = disconnect.PRIOR_REPORT_SHA256
        return proof

    def test_limits_scope_and_inherited_motion_stop_methods_unchanged(self):
        args = hub.arguments(['--prior-report', 'old'])
        driver = self.driver(args.wall_seconds)
        self.assertEqual((driver.args.wall_seconds, driver.args.leg_seconds), (600, 1200))
        self.assertEqual(driver.deadline-driver.work_end, 150)
        self.assertEqual(driver.report['limits']['moving_observation_wall_seconds'], 90)
        self.assertEqual(driver.report['limits']['maximum_navigation_tasks'], 12)
        self.assertEqual(driver.report['limits']['maximum_segments'], 3)
        self.assertEqual(driver.report['qualification_version'], 'smart-nav-20261013.3')
        self.assertIs(hub.HubDisconnectQualification.service_exit, navigation.Qualification.service_exit)
        self.assertIs(hub.HubDisconnectQualification.moving, navigation.Qualification.moving)
        self.assertIs(hub.HubDisconnectQualification.cleanup, navigation.Qualification.cleanup)
        self.assertIs(hub.HubDisconnectQualification.api, disconnect.DisconnectQualification.api)
        for wall in (True, 299, 601, float('nan')):
            with self.subTest(wall=wall), self.assertRaisesRegex(ValueError, 'budget_out_of_bounds'):
                self.driver(wall)

    def test_exact_closed_prior43_hash_counts_and_real_stop_contract(self):
        path = self.root/hub.PRIOR_REPORT_RELATIVE; path.parent.mkdir(parents=True)
        raw = json.dumps(self.prior()).encode(); path.write_bytes(raw)
        with patch.object(hub, 'PRIOR_REPORT_SHA256', hashlib.sha256(raw).hexdigest()):
            proof = hub.validate_prior_report(self.root, path, 121.)
            self.assertEqual(proof['counts'], {'passed': 1, 'failed': 1, 'NA': 7})
            self.assertTrue(proof['stop_record_proof']['passed'])
            with self.assertRaisesRegex(ValueError, 'closed_before_this_driver'):
                hub.validate_prior_report(self.root, path, 119.)
            path.write_bytes(raw+b' ')
            with self.assertRaisesRegex(ValueError, 'sha256_mismatch'):
                hub.validate_prior_report(self.root, path, 121.)
        with self.assertRaisesRegex(ValueError, 'exact_closed_project'):
            hub.validate_prior_report(self.root, self.root/'other.json', 121.)

    def test_prior_bridge_failure_other_error_source_change_and_fake_stop_refused(self):
        changes = [lambda p: p['cases'][-2].update(status='failed'),
            lambda p: p['failure'].update(error='different error'),
            lambda p: p['source_sha256'].update({'mhs/smart_disconnect.py': 'other'}),
            lambda p: p.update(cleanup_errors=['restore_failed']), lambda p: p.update(source_unchanged=False),
            lambda p: p['stop_record_proof'].update(passed=False),
            lambda p: p['final_stop']['state'].update(operator_stopped=True)]
        path = self.root/hub.PRIOR_REPORT_RELATIVE; path.parent.mkdir(parents=True)
        for change in changes:
            prior = self.prior(); change(prior); raw = json.dumps(prior).encode(); path.write_bytes(raw)
            with patch.object(hub, 'PRIOR_REPORT_SHA256', hashlib.sha256(raw).hexdigest()):
                with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'contract_failed'):
                    hub.validate_prior_report(self.root, path, 121.)

    def test_frozen_base_and43_pin_checked_before_original_preflight(self):
        driver = self.driver()
        for name in ('mhs/smart_navigation.py', *hub.SOURCE43_FILES):
            path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'changed')
        with patch.object(navigation.Qualification, 'preflight', new=AsyncMock()) as original:
            with self.assertRaisesRegex(ValueError, 'frozen_source43_or_source29_sha256_mismatch'):
                complete_immediate(driver.preflight())
            original.assert_not_called()

    def test_one_original_segment_gate_with_correct_case_label(self):
        driver = self.driver()
        async def inherited(owner, index): owner.segment_records.append({'segment': index, 'cases': ['original']})
        with patch.object(navigation.Qualification, 'begin_segment', new=AsyncMock(side_effect=inherited)) as original:
            complete_immediate(driver.begin_segment(1))
        original.assert_awaited_once_with(driver, 1)
        self.assertEqual(driver.segment_records[-1]['cases'], ['hub_exit_guard'])
        with self.assertRaisesRegex(ValueError, 'only_one_segment'):
            complete_immediate(driver.begin_segment(2))

    def test_subset_requires_one_task_one_attempt_real_stop_and_zero_model(self):
        proof = self.proof(); self.assertTrue(hub.requested_subset_passed(proof))
        changes = [lambda p: p.update(agent_model_requests_in_qualification_session=1),
            lambda p: p.update(source_unchanged=False), lambda p: p.update(cleanup_errors=['restore_failed']),
            lambda p: p.update(stop_record_proof={'passed': False}), lambda p: p.update(failure={'case': 'late'}),
            lambda p: p['final_stop']['state']['navigation_tasks'].update({'other': {}}),
            lambda p: p['final_stop']['state']['smart_ward']['navigation_qualification'].update(attempts=2),
            lambda p: p['final_stop']['scene_physical'].update(speed_mps=.03)]
        for change in changes:
            candidate = copy.deepcopy(proof); change(candidate)
            with self.subTest(change=change): self.assertFalse(hub.requested_subset_passed(candidate))

    def setup_execute(self, driver):
        driver.preflight = AsyncMock(); driver.begin_segment = AsyncMock(); driver.end_segment = AsyncMock()
        driver.connect_hub = AsyncMock(); driver.service_exit = AsyncMock(return_value={'actual': 'stop'})
        source = self.root/'source'; source.write_bytes(b'frozen')
        driver.report['source_sha256'] = {'source': disconnect.digest(source)}
        async def cleanup():
            driver.report.update({key: value for key, value in self.proof().items() if key not in {'cases', 'status'}})
            driver.submitted_stop = self.fixture.submitted()
        driver.cleanup = AsyncMock(side_effect=cleanup)

    def test_single_original_daemon_to_b_call_one_pass_eight_na(self):
        driver = self.driver(); self.setup_execute(driver)
        self.assertEqual(complete_immediate(driver.execute()), 2)
        driver.service_exit.assert_awaited_once_with('daemon', 'station_b')
        driver.begin_segment.assert_awaited_once_with(1); driver.end_segment.assert_awaited_once_with(1)
        self.assertEqual(driver.report['status'], 'partial_NA'); self.assertTrue(driver.report['requested_subset_passed'])
        self.assertFalse(driver.report['full_plan_acceptance_passed'])
        self.assertEqual(sum(row['status'] == 'NA' for row in driver.results), 8)

    def test_first_failure_no_retry_cleanup_preserved(self):
        driver = self.driver(); driver.claimed = True; self.setup_execute(driver)
        driver.service_exit.side_effect = TimeoutError('actual_scene_and_odometry_motion_above_0_1_mps_not_observed')
        self.assertEqual(complete_immediate(driver.execute()), 1)
        driver.service_exit.assert_awaited_once_with('daemon', 'station_b'); driver.cleanup.assert_awaited_once()
        self.assertFalse(driver.report['requested_subset_passed'])
        self.assertEqual(driver.report['failure']['case'], 'hub_exit_guard')

    def test_late_stop_result_cannot_pass_even_when_returning_real_stop_evidence(self):
        driver = self.driver(); driver.claimed = True; self.setup_execute(driver)
        driver.work_end = 100.
        with patch.object(navigation.time, 'monotonic', return_value=100.1):
            self.assertEqual(complete_immediate(driver.execute()), 1)
        driver.cleanup.assert_awaited_once()
        driver.end_segment.assert_not_called()
        self.assertFalse(driver.report['requested_subset_passed'])
        self.assertIn('qualification_wall_budget_exhausted', driver.report['failure']['error'])


if __name__ == '__main__':
    unittest.main()
