"""Two independent real project Bridge/Hub exit checks, with no Agent calls.

The prior source41 result remains a failed motion-observation precondition:
it did not exit the Bridge process. This qualification starts from a fresh run
and inherits source29's unmodified motion, process ownership and stop gates.
"""
import argparse
import asyncio
import hashlib
import json
import math
import signal
import socket
import time
import traceback
from pathlib import Path

from . import smart_navigation as navigation


QUALIFICATION_SCOPE_VERSION = 'smart-disconnect-20261013.1'
BASE_SOURCE_SHA256 = 'ea3865fd48f206ab50c3927e7831bce0dbd6a9f80d2af76f4406f07dd37b9fb4'
PRIOR_REPORT_SHA256 = '1d1ae0e4b1349e07d89807b40bebc65a4cd315522686fdba014770750067dffb'
PRIOR_REPORT_RELATIVE = ('runs/smart-ward-v1/acceptance/'
    'navigation-guards-source41-1791858854939786913-8f5212770fca/report.json')
PRIOR_SOURCE41_SHA256 = 'ce2b0706580dbeb7c08508fafe117d6941840edf8452bddea1cfbc1295e2923c'
ORIGINAL_SOURCE29_REPORT_SHA256 = 'ee2018e2cf413665406eab008a59394ea214ce62aa5dfb54a31c2e2666070d9c'
MAX_WALL_SECONDS = 900
LEG_SECONDS = 1200
CLEANUP_SECONDS = 150
CASES = ('bridge_exit_guard', 'hub_exit_guard')
EXCLUDED_CASES = tuple(name for name in navigation.ALL_CASES if name not in CASES)
OWN_SOURCES = ('mhs/smart_disconnect.py', 'mhs/test_smart_disconnect.py')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def stop_record_proof(state, run_id, submitted, started_wall):
    """The production stop field is a record, not a boolean.

Bind the record to this run's actual submitted stop operation and corresponding
public event. A truthy arbitrary object or a boolean is not sufficient.
"""
    record = state.get('operator_stopped')
    valid = isinstance(record, dict) and isinstance(submitted, dict)
    operation = submitted.get('operation_id') if isinstance(submitted, dict) else None
    checks = {
        'same_run': state.get('run_id') == run_id and submitted.get('expected_run_id') == run_id if isinstance(submitted, dict) else False,
        'actual_submitted_stop_operation': valid and isinstance(operation, str) and operation.startswith('nav-qual-stop-')
            and record.get('operation_id') == operation,
        'stop_record_times': valid and finite(record.get('wall_time')) and finite(record.get('sim_time'))
            and finite(started_wall) and record['wall_time'] >= started_wall and record['sim_time'] >= 0,
        'stop_record_reason': valid and record.get('reason') == submitted.get('reason') == 'navigation_qualification_finished',
    }
    matching = []
    if all(checks.values()):
        matching = [event for event in state.get('events', []) if event.get('kind') == 'operator_stopped'
            and event.get('operation_id') == operation and event.get('reason') == record['reason']
            and finite(event.get('wall_time')) and event['wall_time'] >= record['wall_time']
            and finite(event.get('sim_time')) and event['sim_time'] == record['sim_time']
            and type(event.get('event_id')) is int and event['event_id'] > 0]
    checks['corresponding_current_run_event'] = bool(matching)
    return {'passed': all(checks.values()), 'checks': checks, 'record': record,
            'submitted_stop': submitted, 'matching_events': matching}


def actual_stop_fields(final):
    state, actual = final.get('state', {}), final.get('scene_physical', {})
    return bool(state.get('stopped_confirmed') is True and state.get('pose_fresh') is True
        and actual.get('available') is True and type(actual.get('stopped_samples')) is int
        and actual['stopped_samples'] >= 3 and finite(actual.get('speed_mps'))
        and finite(actual.get('angular_speed_rad_s')) and 0 <= actual['speed_mps'] < .03
        and abs(actual['angular_speed_rad_s']) < .03 and final.get('watch_disabled') is True
        and not state.get('motion_guard', {}).get('armed')
        and (not state.get('navigation') or state['navigation'].get('status') in navigation.TERMINAL))


def validate_prior_report(root, name, started_wall):
    path = Path(name)
    if not path.is_absolute():
        path = root/path
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('prior_source41_symlink_refused')
    path = path.resolve()
    if path != (root/PRIOR_REPORT_RELATIVE).resolve() or not path.is_file():
        raise ValueError('explicit_closed_project_source41_report_required')
    if path.stat().st_size > 50*1024*1024:
        raise ValueError('prior_source41_size_out_of_bounds')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PRIOR_REPORT_SHA256:
        raise ValueError('prior_source41_report_sha256_mismatch')
    prior = json.loads(raw)
    rows = prior.get('cases', [])
    counts = {key: sum(row.get('status') == key for row in rows) for key in ('passed', 'failed', 'NA')}
    final = prior.get('final_stop', {})
    stop = final.get('state', {}).get('operator_stopped')
    historical_submission = {'expected_run_id': prior.get('run_id'),
        'operation_id': stop.get('operation_id') if isinstance(stop, dict) else None,
        'reason': 'navigation_qualification_finished'}
    stop_proof = stop_record_proof(final.get('state', {}), prior.get('run_id'), historical_submission, prior.get('started_wall'))
    checks = {
        'closed_before_this_driver': finite(prior.get('ended_wall')) and prior['ended_wall'] < started_wall,
        'failed_motion_observation_precondition': prior.get('status') == 'failed_or_incomplete'
            and prior.get('failure', {}).get('case') == 'bridge_exit_guard'
            and 'actual_scene_and_odometry_motion_above_0_1_mps_not_observed' in prior.get('failure', {}).get('error', ''),
        'original_counts': counts == {'passed': 2, 'failed': 1, 'NA': 6}
            and len(rows) == 9 and {row.get('case') for row in rows} == set(navigation.ALL_CASES),
        'cancel_and_change_goal_passed': {row.get('case') for row in rows if row.get('status') == 'passed'}
            == {'stop_real_motion', 'change_goal_after_stop'},
        'same_frozen_sources': prior.get('qualification_version') == navigation.QUALIFICATION_VERSION
            and prior.get('qualification_scope_version') == 'smart-nav-guards-20261013.1'
            and prior.get('source_sha256', {}).get('mhs/smart_navigation.py') == BASE_SOURCE_SHA256
            and prior.get('source_sha256', {}).get('mhs/smart_navigation_guard.py') == PRIOR_SOURCE41_SHA256,
        'original_source29_retained': prior.get('prior_report', {}).get('sha256') == ORIGINAL_SOURCE29_REPORT_SHA256,
        'prior_sources_unchanged': prior.get('source_unchanged') is True,
        'cleanup_and_real_stop': prior.get('cleanup_errors') == [] and actual_stop_fields(final) and stop_proof['passed'],
        'no_Agent_calls': prior.get('agent_model_requests_in_qualification_session') == 0,
    }
    if not all(checks.values()):
        raise ValueError('prior_source41_contract_failed:'+','.join(key for key, passed in checks.items() if not passed))
    return {'path': str(path), 'sha256': PRIOR_REPORT_SHA256, 'counts': counts,
            'checks': checks, 'stop_record_proof': stop_proof, 'original_failure': prior['failure'],
            'interpretation': 'Motion was not observed within the unchanged 90-wall-second precondition; this case did not exit the Bridge. No claim of Nav2 hardware failure or actual disconnect failure. Original source41/source29 results retained.'}


def requested_subset_passed(report):
    final = report.get('final_stop', {}); state = final.get('state', {})
    ward = state.get('smart_ward', {}); q = ward.get('navigation_qualification', {})
    passed = {row.get('case') for row in report.get('cases', []) if row.get('status') == 'passed' and row.get('pass') is True}
    return bool(report.get('status') == 'passed' and not report.get('failure') and set(CASES) <= passed
        and report.get('cleanup_errors') == [] and report.get('source_unchanged') is True
        and report.get('agent_model_requests_in_qualification_session') == 0
        and report.get('stop_record_proof', {}).get('passed') is True and actual_stop_fields(final)
        and q.get('active') is False and q.get('version') == navigation.QUALIFICATION_VERSION and q.get('attempts') == 2
        and len(state.get('navigation_tasks', {})) == 2 and not state.get('orders')
        and not ward.get('watch', {}).get('enabled') and not ward.get('specimens') and not ward.get('incidents'))


class DisconnectQualification(navigation.Qualification):
    def __init__(self, args):
        if type(args.wall_seconds) is not int or not 300 <= args.wall_seconds <= MAX_WALL_SECONDS:
            raise ValueError('disconnect_qualification_budget_out_of_bounds')
        super().__init__(argparse.Namespace(**{**vars(args), 'leg_seconds': LEG_SECONDS}))
        self.submitted_stop = None
        self.report.update(scope='independent real Bridge/Hub disconnect safety qualification; NOT Agnes or full navigation acceptance',
            qualification_scope_version=QUALIFICATION_SCOPE_VERSION, requested_cases=list(CASES),
            excluded_cases=list(EXCLUDED_CASES), requested_subset_passed=False, full_plan_acceptance_passed=False)
        self.report['limits'].update(maximum_driver_wall_seconds=MAX_WALL_SECONDS,
            cleanup_reserve_wall_seconds=CLEANUP_SECONDS, maximum_driver_segments=1, maximum_driver_navigation_calls=2,
            moving_observation_wall_seconds=90, backend_limits_unchanged=True)
        self.report['budget_revision'] = {'reason': 'Separate two-case disconnect qualification from a fresh initial pose after closed source41 motion-observation precondition failure; original failures retained.',
            'backend_qualification_version': navigation.QUALIFICATION_VERSION, 'backend_limits_unchanged': True,
            'Agent_budget_unchanged': '60 requests/2400 sim seconds/3600 wall seconds'}

    async def api(self, path='/api/state', arguments=None, *, cleanup=False):
        # Observe the actual inherited request; never generate or replace an ID.
        if path == '/api/admin/stop-run' and isinstance(arguments, dict):
            self.submitted_stop = dict(arguments)
        return await super().api(path, arguments, cleanup=cleanup)

    async def preflight(self):
        if digest(self.root/'mhs/smart_navigation.py') != BASE_SOURCE_SHA256:
            raise ValueError('frozen_source29_navigation_sha256_mismatch')
        self.report['prior_report'] = validate_prior_report(self.root, self.args.prior_report, self.started_wall)
        from . import manage
        if manage.process('workbench'):
            raise RuntimeError('project_workbench_must_be_stopped_before_qualification')
        with socket.socket() as probe:
            probe.settimeout(.5)
            if probe.connect_ex(('127.0.0.1', 4194)) == 0:
                raise RuntimeError('workbench_facade_port_4194_must_be_closed')
        await super().preflight()
        ward = self.last_state.get('smart_ward', {})
        if ward.get('fault_qualification') or ward.get('operations') or ward.get('model_request_ids') or self.last_state.get('mhs_budget'):
            raise RuntimeError('disconnect_qualification_requires_fresh_reset_without_prior_operations_or_budget')
        self.report['source_sha256'].update({name: digest(self.root/name) for name in OWN_SOURCES})
        self.save()

    async def begin_segment(self, index):
        if index != 1:
            raise ValueError('disconnect_qualification_has_only_one_segment')
        await super().begin_segment(index)
        self.segment_records[-1]['cases'] = list(CASES)
        self.save()

    async def execute(self):
        current = 'preflight'
        try:
            await self.preflight()
            current = 'segment_1_begin'; await self.begin_segment(1)
            await self.connect_hub()
            for current, service, station in (('bridge_exit_guard', 'bridge', 'station_b'),
                                               ('hub_exit_guard', 'daemon', 'station_c')):
                details = await self.service_exit(service, station)
                self.remaining()  # Late physical stop/reconnect cannot qualify.
                self.passed(current, details)
            current = 'segment_1_end'; await self.end_segment(1)
            self.remaining(segment=False)
            self.report['status'] = 'passed'  # Provisional until inherited cleanup is verified.
        except Exception as error:
            self.report.update(status='NA' if not self.claimed else 'failed_or_incomplete',
                failure={'case': current, 'error': repr(error), 'traceback': traceback.format_exc(), 'wall_time': time.time()})
            self.results.append({'case': current, 'status': 'failed' if self.claimed else 'NA', 'pass': False, 'error': repr(error)})
            self.record('qualification_failure', **self.report['failure'])
        finally:
            completed = {row['case'] for row in self.results}
            for name in CASES:
                if name not in completed:
                    self.results.append({'case': name, 'status': 'NA', 'pass': None,
                        'reason': 'Not executed after first failure or finite budget; no retry.'})
            for name in EXCLUDED_CASES:
                self.results.append({'case': name, 'status': 'NA', 'pass': None,
                    'reason': 'Excluded from this two-case qualification; source29/source41 original reports retained.'})
            await self.cleanup()
            sources = self.report.get('source_sha256', {})
            self.report['source_unchanged'] = bool(sources) and all(
                (self.root/name).is_file() and digest(self.root/name) == expected for name, expected in sources.items())
            state = self.report.get('final_stop', {}).get('state', {})
            self.report['stop_record_proof'] = stop_record_proof(state, self.run_id, self.submitted_stop, self.started_wall)
            self.report['scope_counters'] = {'navigation_tasks_observed': len(state.get('navigation_tasks', {})),
                'backend_navigation_attempts': state.get('smart_ward', {}).get('navigation_qualification', {}).get('attempts'),
                'segments_recorded': len(self.segment_records), 'segments_ended': sum('ended_wall' in row for row in self.segment_records),
                'selected_session_model_requests': self.report.get('agent_model_requests_in_qualification_session')}
            self.report['requested_subset_passed'] = requested_subset_passed(self.report)
            if self.report['requested_subset_passed']:
                self.report['status'] = 'partial_NA'
            elif self.report.get('status') == 'passed':
                self.report['status'] = 'failed_or_incomplete'
                self.report['failure'] = {'case': 'final_proof', 'error': 'subset_stop_record_cleanup_source_or_zero_model_proof_failed'}
            self.report.update(ended_wall=time.time(), elapsed_wall_seconds=time.monotonic()-self.started)
            self.save(); print('EVIDENCE '+str(self.directory), flush=True)
        return 2 if self.report['status'] in {'partial_NA', 'NA'} else 1


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='Unique new project evidence directory; existing paths refused.')
    parser.add_argument('--prior-report', required=True, help='Explicit closed source41 report with exact frozen SHA.')
    parser.add_argument('--wall-seconds', type=int, default=MAX_WALL_SECONDS,
                        choices=range(300, MAX_WALL_SECONDS+1), metavar='300..900')
    return parser.parse_args(argv)


async def run(options):
    test = DisconnectQualification(options)
    loop = asyncio.get_running_loop()
    for name in ('SIGINT', 'SIGTERM'):
        if hasattr(signal, name):
            try:
                loop.add_signal_handler(getattr(signal, name), setattr, test, 'interrupted', True)
            except (NotImplementedError, RuntimeError):
                pass
    return await test.execute()


def main(argv=None):
    options = arguments(argv)
    return asyncio.run(run(options))


if __name__ == '__main__':
    raise SystemExit(main())
