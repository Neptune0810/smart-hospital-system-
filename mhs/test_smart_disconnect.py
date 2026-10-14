"""Real API data shapes tested with pure/mocked code, never live services."""
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
from .test_smart_navigation import complete_immediate

ROOT = Path(__file__).resolve().parents[1]


class DisconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'.cache')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def driver(self, wall=900):
        with patch.object(navigation, 'ROOT', self.root):
            driver = disconnect.DisconnectQualification(argparse.Namespace(
                wall_seconds=wall, output=None, prior_report='unused'))
        driver.run_id = 'run'; driver.started_wall = 100.
        return driver

    def submitted(self):
        return {'expected_run_id': 'run', 'operation_id': 'nav-qual-stop-actual',
                'reason': 'navigation_qualification_finished'}

    def state(self):
        record = {'operation_id': self.submitted()['operation_id'], 'wall_time': 110.,
                  'sim_time': 12., 'reason': 'navigation_qualification_finished'}
        return {'run_id': 'run', 'operator_stopped': record, 'pose_fresh': True,
            'stopped_confirmed': True, 'motion_guard': {'armed': False},
            'navigation_tasks': {'1': {}, '2': {}}, 'orders': {},
            'events': [{'kind': 'operator_stopped', 'event_id': 8, **record}],
            'smart_ward': {'specimens': {}, 'incidents': {}, 'watch': {'enabled': False},
                'navigation_qualification': {'active': False, 'version': navigation.QUALIFICATION_VERSION, 'attempts': 2}}}

    def final(self):
        return {'state': self.state(), 'watch_disabled': True, 'scene_physical': {
            'available': True, 'stopped_samples': 3, 'speed_mps': .001, 'angular_speed_rad_s': 0.}}

    def proof(self):
        return {'run_id': 'run', 'status': 'passed', 'cases': [{'case': name, 'status': 'passed', 'pass': True} for name in disconnect.CASES],
            'cleanup_errors': [], 'source_unchanged': True, 'agent_model_requests_in_qualification_session': 0,
            'stop_record_proof': disconnect.stop_record_proof(self.state(), 'run', self.submitted(), 100.),
            'final_stop': self.final()}

    def prior(self):
        rows = [{'case': name, 'status': 'NA', 'pass': None} for name in navigation.ALL_CASES]
        for row in rows:
            if row['case'] in {'stop_real_motion', 'change_goal_after_stop'}:
                row.update(status='passed', **{'pass': True})
            if row['case'] == 'bridge_exit_guard': row.update(status='failed', **{'pass': False})
        return {'run_id': 'run', 'started_wall': 100., 'ended_wall': 120., 'status': 'failed_or_incomplete',
            'failure': {'case': 'bridge_exit_guard', 'error': "TimeoutError('actual_scene_and_odometry_motion_above_0_1_mps_not_observed')"},
            'cases': rows, 'cleanup_errors': [], 'source_unchanged': True,
            'final_stop': self.final(), 'agent_model_requests_in_qualification_session': 0,
            'qualification_version': navigation.QUALIFICATION_VERSION,
            'qualification_scope_version': 'smart-nav-guards-20261013.1',
            'source_sha256': {'mhs/smart_navigation.py': disconnect.BASE_SOURCE_SHA256,
                'mhs/smart_navigation_guard.py': disconnect.PRIOR_SOURCE41_SHA256},
            'prior_report': {'sha256': disconnect.ORIGINAL_SOURCE29_REPORT_SHA256}}

    def test_real_stop_record_with_exact_operation_and_event_qualifies(self):
        result = disconnect.stop_record_proof(self.state(), 'run', self.submitted(), 100.)
        self.assertTrue(result['passed']); self.assertEqual(len(result['matching_events']), 1)

    def test_boolean_empty_stale_wrong_run_operation_reason_and_event_refused(self):
        changes = [lambda s: s.update(operator_stopped=True), lambda s: s.update(operator_stopped=False),
            lambda s: s.update(operator_stopped={}), lambda s: s.update(run_id='other'),
            lambda s: s['operator_stopped'].update(operation_id='wrong'),
            lambda s: s['operator_stopped'].update(wall_time=99.),
            lambda s: s['operator_stopped'].update(wall_time=float('nan')),
            lambda s: s['operator_stopped'].update(reason='another_stop'),
            lambda s: s.update(events=[]), lambda s: s['events'][0].update(operation_id='other'),
            lambda s: s['events'][0].update(sim_time=13.)]
        for change in changes:
            state = self.state(); change(state)
            with self.subTest(change=change):
                self.assertFalse(disconnect.stop_record_proof(state, 'run', self.submitted(), 100.)['passed'])

    def test_prior_report_requires_exact_hash_closed_original_and_real_stop(self):
        path = self.root/disconnect.PRIOR_REPORT_RELATIVE; path.parent.mkdir(parents=True)
        prior = self.prior(); raw = json.dumps(prior).encode(); path.write_bytes(raw)
        with patch.object(disconnect, 'PRIOR_REPORT_SHA256', hashlib.sha256(raw).hexdigest()):
            result = disconnect.validate_prior_report(self.root, path, 121.)
            self.assertEqual(result['counts'], {'passed': 2, 'failed': 1, 'NA': 6})
            with self.assertRaisesRegex(ValueError, 'closed_before_this_driver'):
                disconnect.validate_prior_report(self.root, path, 119.)
            path.write_bytes(raw+b' ')
            with self.assertRaisesRegex(ValueError, 'sha256_mismatch'):
                disconnect.validate_prior_report(self.root, path, 121.)
        with self.assertRaisesRegex(ValueError, 'explicit_closed_project'):
            disconnect.validate_prior_report(self.root, self.root/'other.json', 121.)

    def test_prior_tampered_reason_source_cleanup_stop_and_counts_refused(self):
        changes = [lambda p: p['failure'].update(case='hub_exit_guard'),
            lambda p: p['source_sha256'].update({'mhs/smart_navigation.py': 'other'}),
            lambda p: p.update(cleanup_errors=['failed']), lambda p: p.update(source_unchanged=False),
            lambda p: p['final_stop']['state'].update(operator_stopped=True),
            lambda p: p['cases'][0].update(status='passed')]
        path = self.root/disconnect.PRIOR_REPORT_RELATIVE; path.parent.mkdir(parents=True)
        for change in changes:
            prior = self.prior(); change(prior); raw = json.dumps(prior).encode(); path.write_bytes(raw)
            with patch.object(disconnect, 'PRIOR_REPORT_SHA256', hashlib.sha256(raw).hexdigest()):
                with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'contract_failed'):
                    disconnect.validate_prior_report(self.root, path, 121.)

    def test_driver_budget_does_not_change_backend_or_motion_observation(self):
        args = disconnect.arguments(['--prior-report', 'original'])
        driver = self.driver(args.wall_seconds)
        self.assertEqual(driver.deadline-driver.work_end, 150)
        self.assertEqual(driver.report['limits']['moving_observation_wall_seconds'], 90)
        self.assertEqual(driver.args.leg_seconds, 1200)
        self.assertEqual(driver.report['limits']['maximum_segments'], 3)
        self.assertEqual(driver.report['limits']['maximum_navigation_tasks'], 12)
        self.assertIs(disconnect.DisconnectQualification.service_exit, navigation.Qualification.service_exit)
        self.assertIs(disconnect.DisconnectQualification.moving, navigation.Qualification.moving)
        self.assertIs(disconnect.DisconnectQualification.cleanup, navigation.Qualification.cleanup)
        for wall in (True, 299, 901, float('nan')):
            with self.subTest(wall=wall), self.assertRaisesRegex(ValueError, 'budget_out_of_bounds'):
                self.driver(wall)

    def test_api_observes_actual_submitted_cleanup_operation_without_replacing(self):
        driver = self.driver(); submitted = self.submitted()
        with patch.object(navigation.Qualification, 'api', new=AsyncMock(return_value={'ok': True})) as original:
            complete_immediate(driver.api('/api/admin/stop-run', submitted, cleanup=True))
        original.assert_awaited_once_with('/api/admin/stop-run', submitted, cleanup=True)
        self.assertEqual(driver.submitted_stop, submitted)

    def test_subset_proof_rejects_missing_stop_model_source_extra_tasks_and_late_failure(self):
        proof = self.proof(); self.assertTrue(disconnect.requested_subset_passed(proof))
        changes = [lambda p: p.update(agent_model_requests_in_qualification_session=1),
            lambda p: p.update(source_unchanged=False), lambda p: p.update(cleanup_errors=['restore_failed']),
            lambda p: p.update(stop_record_proof={'passed': False}), lambda p: p.update(failure={'case': 'late'}),
            lambda p: p['final_stop']['state']['navigation_tasks'].update({'3': {}}),
            lambda p: p['final_stop']['scene_physical'].update(speed_mps=.03)]
        for change in changes:
            candidate = copy.deepcopy(proof); change(candidate)
            with self.subTest(change=change): self.assertFalse(disconnect.requested_subset_passed(candidate))

    def test_segment_labels_are_replaced_after_inherited_gate_only(self):
        driver = self.driver()
        async def gate(index): driver.segment_records.append({'segment': index, 'cases': ['original']})
        with patch.object(navigation.Qualification, 'begin_segment', new=AsyncMock(side_effect=gate)) as original:
            complete_immediate(driver.begin_segment(1))
        original.assert_awaited_once_with(1)
        self.assertEqual(driver.segment_records[-1]['cases'], list(disconnect.CASES))
        with self.assertRaisesRegex(ValueError, 'only_one_segment'):
            complete_immediate(driver.begin_segment(2))

    def setup_execute(self, driver):
        driver.preflight = AsyncMock(); driver.begin_segment = AsyncMock(); driver.end_segment = AsyncMock()
        driver.connect_hub = AsyncMock(); driver.service_exit = AsyncMock(return_value={'real': 'stop'})
        source = self.root/'source'; source.write_bytes(b'frozen')
        driver.report['source_sha256'] = {'source': disconnect.digest(source)}
        async def cleanup():
            driver.report.update({key: value for key, value in self.proof().items() if key not in {'cases', 'status'}})
            driver.submitted_stop = self.submitted()
        driver.cleanup = AsyncMock(side_effect=cleanup)

    def test_execute_only_two_disconnects_with_two_pass_seven_na(self):
        driver = self.driver(); self.setup_execute(driver)
        self.assertEqual(complete_immediate(driver.execute()), 2)
        self.assertEqual([call.args for call in driver.service_exit.await_args_list], [('bridge', 'station_b'), ('daemon', 'station_c')])
        self.assertEqual(driver.report['status'], 'partial_NA'); self.assertTrue(driver.report['requested_subset_passed'])
        self.assertFalse(driver.report['full_plan_acceptance_passed'])
        self.assertEqual(sum(row['status'] == 'NA' for row in driver.results), 7)

    def test_failed_motion_precondition_has_no_retry_and_cleanup_still_runs(self):
        driver = self.driver(); driver.claimed = True; self.setup_execute(driver)
        driver.service_exit.side_effect = TimeoutError('actual_scene_and_odometry_motion_above_0_1_mps_not_observed')
        self.assertEqual(complete_immediate(driver.execute()), 1)
        driver.service_exit.assert_awaited_once_with('bridge', 'station_b'); driver.cleanup.assert_awaited_once()
        self.assertFalse(driver.report['requested_subset_passed'])
        self.assertEqual(sum(row['status'] == 'NA' for row in driver.results), 8)


if __name__ == '__main__':
    unittest.main()
