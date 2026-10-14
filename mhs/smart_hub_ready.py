"""Independent Hub-only qualification with bounded read-only startup readiness.

Only connection readiness differs from frozen source47. Motion, process exit,
physical stop, no-resume checks and the 600/450/150 wall budget are unchanged.
No navigation, stop, restore, command, or qualification is retried.
"""
import argparse
import asyncio
import hashlib
import json
import signal
import socket
import time
from pathlib import Path

from . import smart_navigation as navigation
from . import smart_disconnect as disconnect
from . import smart_hub_disconnect as hub


QUALIFICATION_SCOPE_VERSION = 'smart-hub-ready-20261013.1'
READINESS_WALL_SECONDS = 20
PRIOR_REPORT_RELATIVE = ('runs/smart-ward-v1/acceptance/'
    'hub-disconnect-source47-1791860871500335897-9669d686e499/report.json')
PRIOR_REPORT_SHA256 = 'e48d9d44d560f172fac5311b2f224cb08fdcceb7a8f3742f2ff6ea2c9b1d5e8b'
SOURCE47_FILES = {
    'mhs/smart_hub_disconnect.py': 'ea2db2a8dbd8dd383b35e5e9b2be4f67a4da9fb641efe3b46f18353aba458f87',
    'mhs/test_smart_hub_disconnect.py': '85fa33f2fbd6721d3fe3d0b21df16d878fd117b2a37d440682aa9abfd0a52088'}
OWN_SOURCES = ('mhs/smart_hub_ready.py', 'mhs/test_smart_hub_ready.py')


def port_listening(timeout):
    """Fixed loopback endpoint; no Hub command or device mutation."""
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex(('127.0.0.1', 4192)) == 0


def validate_prior_report(root, name, started_wall):
    path = Path(name)
    if not path.is_absolute(): path = root/path
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('prior_source47_symlink_refused')
    path = path.resolve()
    if path != (root/PRIOR_REPORT_RELATIVE).resolve() or not path.is_file():
        raise ValueError('exact_closed_project_source47_report_required')
    if path.stat().st_size > 50*1024*1024:
        raise ValueError('prior_source47_size_out_of_bounds')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PRIOR_REPORT_SHA256:
        raise ValueError('prior_source47_report_sha256_mismatch')
    prior = json.loads(raw); rows = prior.get('cases', [])
    counts = {key: sum(row.get('status') == key for row in rows) for key in ('passed', 'failed', 'NA')}
    final = prior.get('final_stop', {}); original_stop = prior.get('stop_record_proof', {})
    stopped = disconnect.stop_record_proof(final.get('state', {}), prior.get('run_id'),
        original_stop.get('submitted_stop'), prior.get('started_wall'))
    checks = {
        'closed_before_new_qualification': disconnect.finite(prior.get('ended_wall')) and prior['ended_wall'] < started_wall,
        'original_failed_connection_refused': prior.get('status') == 'failed_or_incomplete'
            and prior.get('failure', {}).get('case') == 'hub_exit_guard'
            and 'ConnectionRefusedError' in prior.get('failure', {}).get('error', ''),
        'original_zero_pass_one_fail_eight_NA': counts == {'passed': 0, 'failed': 1, 'NA': 8}
            and len(rows) == 9 and {row.get('case') for row in rows} == set(navigation.ALL_CASES),
        'frozen_source47_and_base': prior.get('qualification_version') == navigation.QUALIFICATION_VERSION
            and prior.get('qualification_scope_version') == hub.QUALIFICATION_SCOPE_VERSION
            and prior.get('source_sha256', {}).get('mhs/smart_navigation.py') == disconnect.BASE_SOURCE_SHA256
            and all(prior.get('source_sha256', {}).get(name) == sha for name, sha in SOURCE47_FILES.items()),
        'source43_original_retained': prior.get('prior_report', {}).get('sha256') == hub.PRIOR_REPORT_SHA256,
        'sources_unchanged': prior.get('source_unchanged') is True,
        'original_cleanup_and_real_stop': prior.get('cleanup_errors') == [] and disconnect.actual_stop_fields(final)
            and original_stop.get('passed') is True and stopped['passed'],
        'original_zero_model': prior.get('agent_model_requests_in_qualification_session') == 0,
    }
    if not all(checks.values()):
        raise ValueError('prior_source47_contract_failed:'+','.join(key for key, passed in checks.items() if not passed))
    return {'path': str(path), 'sha256': PRIOR_REPORT_SHA256, 'counts': counts, 'checks': checks,
        'stop_record_proof': stopped, 'original_failure': prior['failure'],
        'original_source43_report_sha256': hub.PRIOR_REPORT_SHA256,
        'interpretation': 'Original source47 remains failed. This new qualification only waits for startup readiness within the original connection and global deadlines; no motion or device command is retried.'}


class HubReadyQualification(hub.HubDisconnectQualification):
    def __init__(self, args):
        super().__init__(args)
        self.report.update(qualification_scope_version=QUALIFICATION_SCOPE_VERSION,
            scope='independent real Hub disconnect with bounded startup readiness; NOT Agnes or full navigation acceptance',
            hub_readiness_checks=[])
        self.report['limits']['hub_readiness_wall_seconds'] = READINESS_WALL_SECONDS
        self.report['budget_revision']['reason'] = 'New independent qualification after source47 reconnect preceded listener readiness. Read-only listener/hello readiness remains within the original 20-second connect and 600/450/150 scope; no motion or process action retry.'

    async def preflight(self):
        expected = {'mhs/smart_navigation.py': disconnect.BASE_SOURCE_SHA256, **hub.SOURCE43_FILES, **SOURCE47_FILES}
        if any(disconnect.digest(self.root/name) != sha for name, sha in expected.items()):
            raise ValueError('frozen_source47_dependencies_sha256_mismatch')
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
            raise RuntimeError('hub_ready_requires_fresh_reset_without_prior_operations_or_budget')
        self.report['source_sha256'].update(expected)
        self.report['source_sha256'].update({name: disconnect.digest(self.root/name) for name in OWN_SOURCES})
        self.save()

    async def connect_hub(self):
        # One absolute deadline per required connection. It never slides on a
        # probe/race and is clipped by the original segment and work deadlines.
        started = time.monotonic()
        end = started + min(READINESS_WALL_SECONDS, self.remaining())
        evidence = {'started_wall': time.time(), 'absolute_deadline_monotonic': end,
            'maximum_wait_wall_seconds': end-started, 'port_probes': 0,
            'native_hello_attempts': 0, 'connected': False, 'observations': []}
        self.report['hub_readiness_checks'].append(evidence)
        self.record('hub_readiness_begin', **evidence)
        while True:
            available = min(end-time.monotonic(), self.remaining())
            if available <= 0:
                raise TimeoutError('Hub_listener_readiness_original_connect_deadline_exhausted')
            listening = port_listening(min(.2, available))
            evidence['port_probes'] += 1
            evidence['observations'].append({'wall_time': time.time(), 'listening': listening})
            self.record('hub_listener_probe', listening=listening, probe=evidence['port_probes'],
                remaining_original_connect_seconds=max(0, end-time.monotonic()))
            available = min(end-time.monotonic(), self.remaining())
            if available <= 0:
                raise TimeoutError('Hub_listener_readiness_original_connect_deadline_exhausted')
            if listening:
                evidence['native_hello_attempts'] += 1
                try:
                    # The original HubClient performs real websocket + hub/hello
                    # and writes its unmodified messages to hub-messages.jsonl.
                    await asyncio.wait_for(navigation.Qualification.connect_hub(self), available)
                except ConnectionRefusedError as error:
                    # The listener can close between the probe and connect.
                    # Retry only before any websocket/reader exists. Never retry
                    # a partially accepted protocol handshake or other error.
                    if self.hub is not None and (hasattr(self.hub, 'ws') or hasattr(self.hub, 'reader')):
                        self.record('hub_partial_connection_retry_refused', error=repr(error))
                        raise
                    self.hub = None
                    evidence['observations'].append({'wall_time': time.time(), 'pre_websocket_refused': repr(error)})
                    self.record('hub_pre_websocket_connection_refused', error=repr(error))
                else:
                    self.remaining()
                    if time.monotonic() >= end:
                        raise TimeoutError('Hub_native_hello_after_original_connect_deadline')
                    evidence.update(connected=True, connected_wall=time.time(),
                        elapsed_wall_seconds=time.monotonic()-started,
                        actual_native_hello_log='hub-messages.jsonl')
                    self.record('hub_readiness_connected', **evidence)
                    return
            available = min(end-time.monotonic(), self.remaining())
            if available <= 0:
                raise TimeoutError('Hub_listener_readiness_original_connect_deadline_exhausted')
            await asyncio.sleep(min(.25, available))


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='Unique new project evidence directory; existing paths refused.')
    parser.add_argument('--prior-report', required=True, help='Exact closed source47 report, hash verified before mutation.')
    parser.add_argument('--wall-seconds', type=int, default=hub.MAX_WALL_SECONDS,
                        choices=range(300, hub.MAX_WALL_SECONDS+1), metavar='300..600')
    return parser.parse_args(argv)


async def run(options):
    test = HubReadyQualification(options)
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
