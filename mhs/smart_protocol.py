"""Bounded, project-only official MHS checks for the eight smart-ward devices.

This runner never enables watch, starts Agnes turns, changes a checker, retries a
device, or treats skipped/missing results as passes. Run on the project server.
"""
import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .build_runtime import SHA
from .manage import ROOT, ENV, MODE, SMART_MODE, DATA_DIR, api, process, spawn, stop_process

DEVICE_PROFILES = [
    ('hospital-robot-01', ['core', 'motion', 'perception', 'streaming', 'maps']),
    ('hospital-cabinet-b', ['core', 'perception', 'streaming', 'maps']),
    ('hospital-cabinet-c', ['core', 'perception', 'streaming', 'maps']),
    ('hospital-cabinet-lab', ['core', 'perception', 'streaming', 'maps']),
    ('hospital-light-301', ['core', 'perception', 'streaming', 'maps']),
    ('hospital-light-303', ['core', 'perception', 'streaming', 'maps']),
    ('hospital-sensor-301', ['core', 'perception', 'streaming', 'maps']),
    ('hospital-sensor-303', ['core', 'perception', 'streaming', 'maps']),
]
MAX_DEVICE_WALL = 600
CHECKER_URL = 'ws://127.0.0.1:8800'


def save(path, value):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_idle_state():
    state = api()
    ward = state.get('smart_ward')
    if MODE != SMART_MODE or not ward:
        raise RuntimeError('smart_ward_runtime_required')
    if ward.get('watch', {}).get('enabled'):
        raise RuntimeError('watch_must_not_be_enabled')
    if any(order.get('status') not in {'delivered', 'failed'} or order.get('loaded')
           for order in state.get('orders', {}).values()):
        raise RuntimeError('active_or_loaded_delivery_run')
    if any(incident.get('status') not in {'resolved', 'unresolved'}
           for incident in ward.get('incidents', [])):
        raise RuntimeError('active_service_incident')
    if not state.get('pose_fresh') or not state.get('physical_pose') or not state.get('stopped_confirmed'):
        raise RuntimeError('fresh_physical_stop_not_confirmed')
    if not state.get('navigation_ready') or not ward.get('scene_fresh'):
        raise RuntimeError('navigation_or_scene_not_ready')
    if set(ward.get('devices', {})) != {device for device, _ in DEVICE_PROFILES}:
        raise RuntimeError('expected_eight_registered_project_devices')
    return state


def port_is_free():
    # Refuse another listener; never kill a process found through its port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # The Linux checker leaves TIME_WAIT connections after it exits. Reuse
        # permits that state while bind still refuses an active listening socket.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', 8800))


def wait_idle(timeout=10):
    deadline = time.monotonic()+timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            return require_idle_state()
        except Exception as error:
            last_error = error
            time.sleep(.2)
    raise RuntimeError('post_check_idle_state_not_confirmed:'+str(last_error))


def sdk_provenance(python):
    source = ROOT/'agent/agh-source-mhs'
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if commit != SHA:
        raise RuntimeError('official_source_commit_mismatch')
    identity = json.loads(subprocess.check_output([python, '-B', '-c',
        'import agnes_mhs,json,pathlib; print(json.dumps({"path":str(pathlib.Path(agnes_mhs.__file__).resolve().parent),"version":getattr(agnes_mhs,"__version__",None)}))'],
        cwd=ROOT, env=ENV, text=True))
    package = Path(identity['path'])
    tracked = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', SHA], cwd=source, text=True).splitlines()
    entries = [name for name in tracked if '/agnes_mhs/' in name and name.endswith('.py')]
    if not entries:
        raise RuntimeError('pinned_sdk_sources_not_found')
    files = []
    for name in entries:
        relative = name.split('/agnes_mhs/', 1)[1]
        installed = package/relative
        expected = hashlib.sha256(subprocess.check_output(['git', 'show', SHA+':'+name], cwd=source)).hexdigest()
        actual = digest(installed) if installed.is_file() else None
        files.append(dict(source=name, installed=str(installed), expected_sha256=expected,
                          installed_sha256=actual, matches=expected == actual))
    return dict(source_commit=commit, expected_commit=SHA, sdk=identity, files=files,
                matches=all(item['matches'] for item in files))


def finish_child(child):
    """Only Popen children created by this invocation can be terminated here."""
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def summarize_report(path, device, profiles):
    if not path.exists():
        return dict(status='NA', reason='official_report_not_written', results=[])
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
        if report.get('device') != device or set(report.get('profiles', [])) != set(profiles):
            raise ValueError('official_report_device_or_profiles_mismatch')
        rows = report.get('results')
        if not isinstance(rows, list) or not rows:
            raise ValueError('official_report_results_missing')
        counts = {name: sum(row.get('status') == name for row in rows) for name in ['pass', 'fail', 'skip']}
        return dict(status='failed' if counts['fail'] else 'reported', counts=counts,
                    reported_total=len(rows),
                    NA=[dict(id=row.get('id'), reason=row.get('evidence', 'not_executed'))
                        for row in rows if row.get('status') not in {'pass', 'fail'}],
                    sha256=digest(path), results=rows)
    except (ValueError, TypeError, AttributeError) as error:
        return dict(status='NA', reason=str(error), sha256=digest(path), results=[])


def run_device(python, directory, device, profiles):
    directory.mkdir(exist_ok=False)
    checker_args = [python, '-B', '-m', 'agnes_mhs.check', '--host', '127.0.0.1', '--port', '8800',
                    '--profile', ','.join(profiles), '--wait', '30', '--replace-wait', '10',
                    '--report', str(directory/'official-report.json')]
    adapter_args = [python, '-B', '-m', 'mhs.bridge']
    adapter_env = dict(ENV, HOSPITAL_MHS_HUB=CHECKER_URL, HOSPITAL_MHS_DEVICE_SELECT=device,
                       HOSPITAL_MHS_CAPTURE_DIR=str(directory/'protocol'))
    item = dict(device=device, profiles_requested=profiles, retries=0, wall_limit=MAX_DEVICE_WALL,
                checker_command=checker_args, adapter_command=adapter_args, test_hub=CHECKER_URL,
                status='NA', started_wall=time.time(), checker_exit=None, adapter_exit=None,
                adapter_termination_requested=False, timeout=False)
    checker = adapter = None
    started = time.monotonic()
    try:
        state = require_idle_state()
        save(directory/'before-state.json', state)
        port_is_free()
        with (directory/'checker.log').open('wb') as log:
            checker = subprocess.Popen(checker_args, cwd=ROOT, env=ENV, stdout=log, stderr=subprocess.STDOUT)
        item['checker_pid'] = checker.pid
        time.sleep(min(1, MAX_DEVICE_WALL))
        if checker.poll() is not None:
            raise RuntimeError('checker_exited_before_adapter_start')
        with (directory/'adapter.log').open('wb') as log:
            adapter = subprocess.Popen(adapter_args, cwd=ROOT, env=adapter_env, stdout=log, stderr=subprocess.STDOUT)
        item['adapter_pid'] = adapter.pid
        # Reserve termination + API/physical-stop time inside the 600-second bound.
        checker.wait(timeout=max(.1, MAX_DEVICE_WALL-(time.monotonic()-started)-55))
    except subprocess.TimeoutExpired:
        item.update(timeout=True, error='checker_wall_limit_reached')
    except (Exception, KeyboardInterrupt) as error:
        item['error'] = str(error) or type(error).__name__
        if isinstance(error, KeyboardInterrupt):
            item['interrupted'] = True
    finally:
        item['adapter_termination_requested'] = bool(adapter and adapter.poll() is None)
        for child in [adapter, checker]:
            try:
                finish_child(child)
            except Exception as error:
                item.setdefault('cleanup_errors', []).append(str(error))
        item['checker_exit'] = checker.poll() if checker else None
        item['adapter_exit'] = adapter.poll() if adapter else None
        report = summarize_report(directory/'official-report.json', device, profiles)
        item['official'] = report
        item['status'] = ('partial_NA' if report.get('NA') else 'passed') if item['checker_exit'] == 0 and report['status'] == 'reported' and not item.get('error') and not item.get('cleanup_errors') else 'failed' if checker else 'NA'
        try:
            api('/api/mhs/stop', {'reason': 'isolated_protocol_device_end'})
            after = wait_idle()
            save(directory/'after-state.json', after)
        except Exception as error:
            item['safety_error'] = str(error)
            item['status'] = 'failed'
        item['ended_wall'] = time.time()
        item['elapsed_wall'] = time.monotonic()-started
        save(directory/'execution.json', item)
    return item


def parse_arguments(argv=None):
    devices = [device for device, _ in DEVICE_PROFILES]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--devices', nargs='+', choices=devices,
                        help='Run only these project devices in a new evidence directory; no automatic retries.')
    arguments = parser.parse_args(argv)
    if arguments.devices and len(arguments.devices) != len(set(arguments.devices)):
        parser.error('--devices must not repeat a device')
    arguments.devices = arguments.devices or devices
    return arguments


def main(argv=None):
    arguments = parse_arguments(argv)
    profiles_by_device = dict(DEVICE_PROFILES)
    selected_profiles = [(device, profiles_by_device[device]) for device in arguments.devices]
    full_suite_selected = set(arguments.devices) == set(profiles_by_device)
    directory = DATA_DIR/'acceptance'/('protocol-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    summary = dict(protocol='smart-ward-v1', source_commit=SHA, started_wall=time.time(),
                   retries=0, maximum_wall_per_device=MAX_DEVICE_WALL,
                   requested_devices=arguments.devices, full_suite_selected=full_suite_selected,
                   unrequested_devices=[device for device in profiles_by_device if device not in arguments.devices],
                   results=[dict(device=device, profiles_requested=profiles, status='NA', reason='not_executed')
                            for device, profiles in selected_profiles], restored_bridge=False)
    python = str(ROOT/'agent/mhs-venv/bin/python')
    stopped_bridge = False
    previous_term = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt('protocol_runner_terminated')

    signal.signal(signal.SIGTERM, interrupted)
    save(directory/'summary.json', summary)
    try:
        save(directory/'before-state.json', require_idle_state())
        provenance = sdk_provenance(python)
        save(directory/'sdk-provenance.json', provenance)
        if not provenance['matches']:
            raise RuntimeError('installed_sdk_differs_from_pinned_official_source')
        port_is_free()
        if not process('bridge'):
            raise RuntimeError('project_bridge_not_running_before_check')
        # An interruption after SIGTERM must still restore our project bridge.
        stopped_bridge = True
        stop_process('bridge')
        for index, (device, profiles) in enumerate(selected_profiles):
            summary['results'][index] = run_device(python, directory/device, device, profiles)
            save(directory/'summary.json', summary)
            if summary['results'][index].get('interrupted'):
                raise KeyboardInterrupt('protocol_runner_interrupted')
            if summary['results'][index].get('safety_error'):
                raise RuntimeError('physical_state_not_safe_to_continue')
    except (Exception, KeyboardInterrupt) as error:
        summary['error'] = str(error) or type(error).__name__
    finally:
        if stopped_bridge:
            try:
                api('/api/mhs/stop', {'reason': 'protocol_acceptance_end'})
            except Exception as error:
                summary['final_stop_error'] = str(error)
            try:
                normal_env = dict(ENV, HOSPITAL_MHS_HUB='ws://127.0.0.1:4192')
                normal_env.pop('HOSPITAL_MHS_DEVICE_SELECT', None)
                normal_env.pop('HOSPITAL_MHS_CAPTURE_DIR', None)
                spawn('bridge', [python, '-m', 'mhs.bridge'], normal_env)
                deadline = time.monotonic()+30
                while time.monotonic() < deadline:
                    state = require_idle_state()
                    if not process('bridge'):
                        raise RuntimeError('restored_bridge_exited')
                    if state.get('motion_guard', {}).get('healthy'):
                        summary['restored_bridge'] = True
                        save(directory/'restored-state.json', state)
                        break
                    time.sleep(.5)
                else:
                    raise RuntimeError('restored_bridge_not_healthy')
            except Exception as error:
                summary['restore_error'] = str(error)
        summary['ended_wall'] = time.time()
        summary['counts'] = {status: sum(item['status'] == status for item in summary['results'])
                             for status in ['passed', 'partial_NA', 'failed', 'NA']}
        summary['check_counts'] = {status: sum(item.get('official', {}).get('counts', {}).get(status, 0)
                                              for item in summary['results'])
                                   for status in ['pass', 'fail', 'skip']}
        summary['NA_check_count'] = sum(len(item.get('official', {}).get('NA', [])) for item in summary['results'])
        summary['passed'] = (summary['full_suite_selected'] and all(item['status'] == 'passed' for item in summary['results'])
                             and summary['restored_bridge'] and not summary.get('error') and not summary.get('final_stop_error'))
        save(directory/'summary.json', summary)
        signal.signal(signal.SIGTERM, previous_term)
        print('EVIDENCE', directory, flush=True)
        print(json.dumps(dict(passed=summary['passed'], counts=summary['counts'], error=summary.get('error'),
                              restore_error=summary.get('restore_error')), ensure_ascii=False), flush=True)
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
