"""One real Hub disconnect check from a fresh run, without Agnes or orders.

Source29/source41/source43 failures remain separate evidence. The closed source43
Hub case never passed its motion precondition and did not exit the daemon. This
driver only executes the original service_exit('daemon', 'station_b') once.
"""
import argparse
import asyncio
import hashlib
import json
import signal
import socket
import time
import traceback
from pathlib import Path

from . import smart_navigation as navigation
from . import smart_disconnect as disconnect


QUALIFICATION_SCOPE_VERSION = 'smart-hub-disconnect-20261013.1'
MAX_WALL_SECONDS = 600
CLEANUP_SECONDS = 150
CASE = 'hub_exit_guard'
EXCLUDED_CASES = tuple(name for name in navigation.ALL_CASES if name != CASE)
PRIOR_REPORT_RELATIVE = ('runs/smart-ward-v1/acceptance/'
    'disconnect-source43-1791860194686622646-1c81b597185a/report.json')
PRIOR_REPORT_SHA256 = 'dabe30c351b85713a8dda9a225d1d0d118acc04f6d5a9970d4b528cee1d62b42'
SOURCE43_FILES = {
    'mhs/smart_disconnect.py': '3229c0b986382e179beb246b77e741b393377bf29940a241ee0c9cca7a5ec2ae',
    'mhs/test_smart_disconnect.py': '56048c567f3ff4b48c8cad99f2dbf014aaa53aff11f51643625664b58b281982'}
OWN_SOURCES = ('mhs/smart_hub_disconnect.py', 'mhs/test_smart_hub_disconnect.py')


def validate_prior_report(root, name, started_wall):
    path = Path(name)
    if not path.is_absolute():
        path = root/path
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('prior_source43_symlink_refused')
    path = path.resolve()
    if path != (root/PRIOR_REPORT_RELATIVE).resolve() or not path.is_file():
        raise ValueError('exact_closed_project_source43_report_required')
    if path.stat().st_size > 50*1024*1024:
        raise ValueError('prior_source43_size_out_of_bounds')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PRIOR_REPORT_SHA256:
        raise ValueError('prior_source43_report_sha256_mismatch')
    prior = json.loads(raw)
    rows = prior.get('cases', [])
    counts = {key: sum(row.get('status') == key for row in rows) for key in ('passed', 'failed', 'NA')}
    final = prior.get('final_stop', {})
    original_stop = prior.get('stop_record_proof', {})
    stop = disconnect.stop_record_proof(final.get('state', {}), prior.get('run_id'),
        original_stop.get('submitted_stop'), prior.get('started_wall'))
    checks = {
        'closed_before_this_driver': disconnect.finite(prior.get('ended_wall')) and prior['ended_wall'] < started_wall,
        'original_one_pass_one_failure_seven_NA': counts == {'passed': 1, 'failed': 1, 'NA': 7}
            and len(rows) == 9 and {row.get('case') for row in rows} == set(navigation.ALL_CASES),
        'Bridge_passed': {row.get('case') for row in rows if row.get('status') == 'passed'} == {'bridge_exit_guard'},
        'Hub_failed_before_exit_motion_precondition': prior.get('status') == 'failed_or_incomplete'
            and prior.get('failure', {}).get('case') == CASE
            and 'actual_scene_and_odometry_motion_above_0_1_mps_not_observed' in prior.get('failure', {}).get('error', ''),
        'same_frozen_source43_and_base': prior.get('qualification_version') == navigation.QUALIFICATION_VERSION
            and prior.get('qualification_scope_version') == disconnect.QUALIFICATION_SCOPE_VERSION
            and prior.get('source_sha256', {}).get('mhs/smart_navigation.py') == disconnect.BASE_SOURCE_SHA256
            and all(prior.get('source_sha256', {}).get(name) == sha for name, sha in SOURCE43_FILES.items()),
        'original_source41_retained': prior.get('prior_report', {}).get('sha256') == disconnect.PRIOR_REPORT_SHA256,
        'sources_unchanged': prior.get('source_unchanged') is True,
        'clean_stop_and_recovery': prior.get('cleanup_errors') == [] and disconnect.actual_stop_fields(final)
            and original_stop.get('passed') is True and stop['passed'],
        'zero_model_requests': prior.get('agent_model_requests_in_qualification_session') == 0,
    }
    if not all(checks.values()):
        raise ValueError('prior_source43_contract_failed:'+','.join(key for key, passed in checks.items() if not passed))
    return {'path': str(path), 'sha256': PRIOR_REPORT_SHA256, 'counts': counts, 'checks': checks,
        'stop_record_proof': stop, 'original_failure': prior['failure'],
        'original_source41_report_sha256': disconnect.PRIOR_REPORT_SHA256,
        'original_source29_report_sha256': disconnect.ORIGINAL_SOURCE29_REPORT_SHA256,
        'interpretation': 'Bridge exit safety passed. Hub daemon was not exited because both linear velocities did not exceed 0.1 m/s within the unchanged 90-wall-second precondition. This new initial-pose Hub-only scope preserves those original results.'}


def requested_subset_passed(report):
    final = report.get('final_stop', {}); state = final.get('state', {})
    ward = state.get('smart_ward', {}); qualification = ward.get('navigation_qualification', {})
    passed = [row for row in report.get('cases', []) if row.get('status') == 'passed' and row.get('pass') is True]
    return bool(report.get('status') == 'passed' and not report.get('failure')
        and len(passed) == 1 and passed[0].get('case') == CASE
        and report.get('cleanup_errors') == [] and report.get('source_unchanged') is True
        and report.get('agent_model_requests_in_qualification_session') == 0
        and report.get('stop_record_proof', {}).get('passed') is True and disconnect.actual_stop_fields(final)
        and qualification.get('active') is False and qualification.get('version') == navigation.QUALIFICATION_VERSION
        and qualification.get('attempts') == 1 and len(state.get('navigation_tasks', {})) == 1
        and not state.get('orders') and not ward.get('watch', {}).get('enabled')
        and not ward.get('incidents') and not ward.get('specimens'))


class HubDisconnectQualification(disconnect.DisconnectQualification):
    def __init__(self, args):
        if type(args.wall_seconds) is not int or not 300 <= args.wall_seconds <= MAX_WALL_SECONDS:
            raise ValueError('hub_disconnect_qualification_budget_out_of_bounds')
        super().__init__(args)
        self.report.update(scope='independent single real Hub disconnect from a fresh initial run; NOT Agnes or full navigation acceptance',
            qualification_scope_version=QUALIFICATION_SCOPE_VERSION, requested_cases=[CASE],
            excluded_cases=list(EXCLUDED_CASES), requested_subset_passed=False, full_plan_acceptance_passed=False)
        self.report['limits'].update(maximum_driver_wall_seconds=MAX_WALL_SECONDS,
            maximum_driver_navigation_calls=1, maximum_driver_segments=1,
            cleanup_reserve_wall_seconds=CLEANUP_SECONDS)
        self.report['budget_revision']['reason'] = 'Fresh initial-pose Hub-only qualification after source43 Hub motion precondition failure; Bridge is not rerun and all original results remain unchanged.'

    async def preflight(self):
        expected = {'mhs/smart_navigation.py': disconnect.BASE_SOURCE_SHA256, **SOURCE43_FILES}
        if any(disconnect.digest(self.root/name) != sha for name, sha in expected.items()):
            raise ValueError('frozen_source43_or_source29_sha256_mismatch')
        self.report['prior_report'] = validate_prior_report(self.root, self.args.prior_report, self.started_wall)
        from . import manage
        if manage.process('workbench'):
            raise RuntimeError('project_workbench_must_be_stopped_before_qualification')
        with socket.socket() as probe:
            probe.settimeout(.5)
            if probe.connect_ex(('127.0.0.1', 4194)) == 0:
                raise RuntimeError('workbench_facade_port_4194_must_be_closed')
        await navigation.Qualification.preflight(self)
        ward = self.last_state.get('smart_ward', {})
        if ward.get('fault_qualification') or ward.get('operations') or ward.get('model_request_ids') or self.last_state.get('mhs_budget'):
            raise RuntimeError('hub_disconnect_requires_fresh_reset_without_prior_operations_or_budget')
        self.report['source_sha256'].update(expected)
        self.report['source_sha256'].update({name: disconnect.digest(self.root/name) for name in OWN_SOURCES})
        self.save()

    async def begin_segment(self, index):
        if index != 1:
            raise ValueError('hub_disconnect_has_only_one_segment')
        await navigation.Qualification.begin_segment(self, index)
        self.segment_records[-1]['cases'] = [CASE]
        self.save()

    async def execute(self):
        current = 'preflight'
        try:
            await self.preflight()
            current = 'segment_1_begin'; await self.begin_segment(1)
            await self.connect_hub()
            current = CASE
            details = await self.service_exit('daemon', 'station_b')
            self.remaining()  # Late motion/stop/reconnect responses cannot qualify.
            self.passed(CASE, details)
            current = 'segment_1_end'; await self.end_segment(1)
            self.remaining(segment=False)
            self.report['status'] = 'passed'
        except Exception as error:
            self.report.update(status='NA' if not self.claimed else 'failed_or_incomplete',
                failure={'case': current, 'error': repr(error), 'traceback': traceback.format_exc(), 'wall_time': time.time()})
            self.results.append({'case': current, 'status': 'failed' if self.claimed else 'NA', 'pass': False, 'error': repr(error)})
            self.record('qualification_failure', **self.report['failure'])
        finally:
            if CASE not in {row['case'] for row in self.results}:
                self.results.append({'case': CASE, 'status': 'NA', 'pass': None,
                    'reason': 'Not executed after first failure or finite budget; no retry.'})
            for name in EXCLUDED_CASES:
                self.results.append({'case': name, 'status': 'NA', 'pass': None,
                    'reason': 'Excluded from single Hub qualification; source29/source41/source43 original results retained.'})
            await self.cleanup()
            sources = self.report.get('source_sha256', {})
            self.report['source_unchanged'] = bool(sources) and all(
                (self.root/name).is_file() and disconnect.digest(self.root/name) == sha for name, sha in sources.items())
            state = self.report.get('final_stop', {}).get('state', {})
            self.report['stop_record_proof'] = disconnect.stop_record_proof(state, self.run_id, self.submitted_stop, self.started_wall)
            self.report['scope_counters'] = {'navigation_tasks_observed': len(state.get('navigation_tasks', {})),
                'backend_navigation_attempts': state.get('smart_ward', {}).get('navigation_qualification', {}).get('attempts'),
                'segments_recorded': len(self.segment_records), 'segments_ended': sum('ended_wall' in row for row in self.segment_records),
                'selected_session_model_requests': self.report.get('agent_model_requests_in_qualification_session')}
            self.report['requested_subset_passed'] = requested_subset_passed(self.report)
            if self.report['requested_subset_passed']:
                self.report['status'] = 'partial_NA'
            elif self.report.get('status') == 'passed':
                self.report['status'] = 'failed_or_incomplete'
                self.report['failure'] = {'case': 'final_proof', 'error': 'subset_stop_cleanup_source_or_zero_model_proof_failed'}
            self.report.update(ended_wall=time.time(), elapsed_wall_seconds=time.monotonic()-self.started)
            self.save(); print('EVIDENCE '+str(self.directory), flush=True)
        return 2 if self.report['status'] in {'partial_NA', 'NA'} else 1


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='Unique new project evidence directory; existing paths refused.')
    parser.add_argument('--prior-report', required=True, help='Exact closed source43 report, hash verified before mutation.')
    parser.add_argument('--wall-seconds', type=int, default=MAX_WALL_SECONDS,
                        choices=range(300, MAX_WALL_SECONDS+1), metavar='300..600')
    return parser.parse_args(argv)


async def run(options):
    test = HubDisconnectQualification(options)
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
