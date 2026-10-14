"""Independent, bounded completion of four real navigation safety checks.

This reuses the frozen production-path source29 qualification unchanged.  It
does not retry or replace its earlier failed result, create orders, or run Agnes.
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


QUALIFICATION_SCOPE_VERSION = 'smart-nav-guards-20261013.1'
BASE_SOURCE_SHA256 = 'ea3865fd48f206ab50c3927e7831bce0dbd6a9f80d2af76f4406f07dd37b9fb4'
PRIOR_REPORT_SHA256 = 'ee2018e2cf413665406eab008a59394ea214ce62aa5dfb54a31c2e2666070d9c'
PRIOR_REPORT_RELATIVE = ('runs/smart-ward-v1/acceptance/'
    'navigation-source29-1791853867204559698-28388e0e0f10/report.json')
MAX_WALL_SECONDS = 1800
LEG_SECONDS = 1200
CLEANUP_SECONDS = 150
SEGMENTS = (('stop_real_motion', 'change_goal_after_stop'),
            ('bridge_exit_guard', 'hub_exit_guard'))
REQUESTED_CASES = tuple(name for segment in SEGMENTS for name in segment)
EXCLUDED_CASES = tuple(name for name in navigation.ALL_CASES if name not in REQUESTED_CASES)
OWN_SOURCES = ('mhs/smart_navigation_guard.py', 'mhs/test_smart_navigation_guard.py')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_prior_report(root, name, started_wall):
    """Read the exact closed original, without manufacturing a closure marker."""
    path = Path(name)
    if not path.is_absolute():
        path = root/path
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('prior_report_symlink_refused')
    path = path.resolve()
    if path != (root/PRIOR_REPORT_RELATIVE).resolve() or not path.is_file():
        raise ValueError('prior_report_must_be_exact_project_source29_report')
    if path.stat().st_size > 50*1024*1024:
        raise ValueError('prior_report_size_out_of_bounds')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PRIOR_REPORT_SHA256:
        raise ValueError('prior_report_sha256_mismatch')
    value = json.loads(raw)
    rows = value.get('cases', [])
    counts = {key: sum(row.get('status') == key for row in rows)
              for key in ('passed', 'failed', 'NA')}
    final = value.get('final_stop', {})
    state, actual = final.get('state', {}), final.get('scene_physical', {})
    stamp = value.get('ended_wall')
    numbers = (actual.get('speed_mps'), actual.get('angular_speed_rad_s'), stamp)
    checks = {
        'exact_nine_cases': len(rows) == 9 and {row.get('case') for row in rows} == set(navigation.ALL_CASES),
        'original_counts': counts == {'passed': 6, 'failed': 1, 'NA': 2},
        'original_failure': value.get('status') == 'failed_or_incomplete'
            and value.get('failure', {}).get('case') == 'change_goal_after_stop',
        'closed_before_this_driver': all(isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) for v in numbers) and stamp < started_wall if isinstance(stamp, (int, float)) else False,
        'same_base_source': value.get('qualification_version') == navigation.QUALIFICATION_VERSION
            and value.get('source_sha256', {}).get('mhs/smart_navigation.py') == BASE_SOURCE_SHA256,
        'cleanup_clean': value.get('cleanup_errors') == [],
        'actual_stop': state.get('stopped_confirmed') is True and state.get('pose_fresh') is True
            and actual.get('available') is True and actual.get('stopped_samples', 0) >= 3
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in numbers[:2])
            and actual.get('speed_mps', math.inf) < .03
            and abs(actual.get('angular_speed_rad_s', math.inf)) < .03
            and not state.get('motion_guard', {}).get('armed')
            and (not state.get('navigation') or state['navigation'].get('status') in navigation.TERMINAL)
            and final.get('watch_disabled') is True,
    }
    if not all(checks.values()):
        raise ValueError('prior_report_contract_failed:'+','.join(k for k, passed in checks.items() if not passed))
    return {'path': str(path), 'sha256': PRIOR_REPORT_SHA256, 'counts': counts,
            'checks': checks, 'original_failure': value['failure'],
            'statement': 'Original source29 failure and two NA cases remain unchanged; this is a separate qualification.'}


def requested_subset_passed(report):
    passed = {row.get('case') for row in report.get('cases', []) if row.get('status') == 'passed' and row.get('pass') is True}
    final = report.get('final_stop', {})
    state = final.get('state', {})
    actual = final.get('scene_physical', {})
    qualification = state.get('smart_ward', {}).get('navigation_qualification', {})
    return bool(report.get('status') == 'passed' and not report.get('failure')
        and set(REQUESTED_CASES) <= passed and report.get('cleanup_errors') == []
        and report.get('source_unchanged') is True
        and report.get('agent_model_requests_in_qualification_session') == 0
        and final.get('watch_disabled') is True and state.get('stopped_confirmed') is True
        and state.get('operator_stopped') is True
        and not state.get('motion_guard', {}).get('armed')
        and actual.get('available') is True and actual.get('stopped_samples', 0) >= 3
        and actual.get('speed_mps', math.inf) < .03
        and abs(actual.get('angular_speed_rad_s', math.inf)) < .03
        and qualification.get('active') is False
        and qualification.get('version') == navigation.QUALIFICATION_VERSION
        and qualification.get('attempts') == 4
        and len(state.get('navigation_tasks', {})) == 4
        and not state.get('orders') and not state.get('smart_ward', {}).get('specimens')
        and not state.get('smart_ward', {}).get('incidents'))


class GuardQualification(navigation.Qualification):
    def __init__(self, args):
        if type(args.wall_seconds) is not int or not 300 <= args.wall_seconds <= MAX_WALL_SECONDS:
            raise ValueError('navigation_guard_budget_out_of_bounds')
        base = argparse.Namespace(**{**vars(args), 'leg_seconds': LEG_SECONDS})
        super().__init__(base)
        self.report.update(scope='independent real cancel/change-goal and project bridge/Hub exit qualification; NOT Agnes or full navigation acceptance',
            qualification_scope_version=QUALIFICATION_SCOPE_VERSION,
            requested_cases=list(REQUESTED_CASES), excluded_cases=list(EXCLUDED_CASES),
            requested_subset_passed=False, full_plan_acceptance_passed=False)
        self.report['limits'].update(wall_seconds=args.wall_seconds,
            cleanup_reserve_wall_seconds=CLEANUP_SECONDS, maximum_driver_navigation_calls=4,
            maximum_driver_segments=2, maximum_driver_wall_seconds=MAX_WALL_SECONDS,
            backend_limits_unchanged=True)
        self.report['budget_revision'] = {
            'reason': 'Separate completion qualification after frozen source29 segment exhaustion; original failure is retained.',
            'backend_qualification_version': navigation.QUALIFICATION_VERSION,
            'backend_limits_unchanged': True, 'Agent_budget_unchanged': '60 requests/2400 sim seconds/3600 wall seconds'}

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
            raise RuntimeError('guard_qualification_requires_fresh_reset_without_prior_operations_or_budget')
        self.report['source_sha256'].update({name: digest(self.root/name) for name in OWN_SOURCES})
        self.save()

    async def begin_segment(self, index):
        # The inherited begin validates the actual production qualification,
        # identity, physical stop and unextended backend/global deadlines.
        if index not in (1, 2):
            raise ValueError('guard_qualification_has_only_two_segments')
        await super().begin_segment(index)
        self.segment_records[-1]['cases'] = list(SEGMENTS[index-1])
        self.save()

    async def execute(self):
        current = 'preflight'
        try:
            await self.preflight()
            for index, cases in enumerate(SEGMENTS, 1):
                current = 'segment_'+str(index)+'_begin'
                await self.begin_segment(index)
                if self.hub is None:
                    await self.connect_hub()
                for name in cases:
                    current = name
                    if name == 'stop_real_motion':
                        details = await self.cancel_motion()
                    elif name == 'change_goal_after_stop':
                        details = await self.arrival('lab')
                    elif name == 'bridge_exit_guard':
                        details = await self.service_exit('bridge', 'station_b')
                    else:
                        details = await self.service_exit('daemon', 'station_c')
                    self.remaining()  # A late stop/restore response cannot pass.
                    self.passed(name, details)
                current = 'segment_'+str(index)+'_end'
                await self.end_segment(index)
                self.remaining(segment=False)
            self.report['status'] = 'passed'  # Only provisional: cleanup still authoritative.
        except Exception as error:
            self.report.update(status='NA' if not self.claimed else 'failed_or_incomplete',
                failure={'case': current, 'error': repr(error), 'traceback': traceback.format_exc(), 'wall_time': time.time()})
            self.results.append({'case': current, 'status': 'failed' if self.claimed else 'NA', 'pass': False, 'error': repr(error)})
            self.record('qualification_failure', **self.report['failure'])
        finally:
            completed = {row['case'] for row in self.results}
            for name in REQUESTED_CASES:
                if name not in completed:
                    self.results.append({'case': name, 'status': 'NA', 'pass': None,
                        'reason': 'Not executed after first failure or finite budget; no retry.'})
            for name in EXCLUDED_CASES:
                self.results.append({'case': name, 'status': 'NA', 'pass': None,
                    'reason': 'Excluded from this independent four-case completion qualification; prior source29 report retained separately.'})
            await self.cleanup()  # Inherited ownership, stop, recovery, recorder and zero-model checks.
            sources = self.report.get('source_sha256', {})
            self.report['source_unchanged'] = bool(sources) and all(
                (self.root/name).is_file() and digest(self.root/name) == expected for name, expected in sources.items())
            final_state = self.report.get('final_stop', {}).get('state', {})
            self.report['scope_counters'] = {
                'navigation_tasks_observed': len(final_state.get('navigation_tasks', {})),
                'backend_navigation_attempts': final_state.get('smart_ward', {}).get('navigation_qualification', {}).get('attempts'),
                'segments_recorded': len(self.segment_records),
                'segments_ended': sum('ended_wall' in row for row in self.segment_records),
                'selected_session_model_requests': self.report.get('agent_model_requests_in_qualification_session')}
            self.report['requested_subset_passed'] = requested_subset_passed(self.report)
            if self.report['requested_subset_passed']:
                self.report['status'] = 'partial_NA'
            elif self.report.get('status') == 'passed':
                self.report['status'] = 'failed_or_incomplete'
                self.report['failure'] = {'case': 'final_proof', 'error': 'requested_subset_or_cleanup_or_source_or_zero_model_proof_failed'}
            self.report.update(ended_wall=time.time(), elapsed_wall_seconds=time.monotonic()-self.started)
            self.save()
            print('EVIDENCE '+str(self.directory), flush=True)
        return 2 if self.report['status'] in {'partial_NA', 'NA'} else 1


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='Unique new project evidence directory; existing paths refused.')
    parser.add_argument('--prior-report', required=True, help='Exact closed source29 report; frozen SHA and original failure are required.')
    parser.add_argument('--wall-seconds', type=int, default=MAX_WALL_SECONDS,
                        choices=range(300, MAX_WALL_SECONDS+1), metavar='300..1800')
    return parser.parse_args(argv)


async def run(options):
    test = GuardQualification(options)
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
