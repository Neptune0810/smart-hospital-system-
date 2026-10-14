#!/usr/bin/env python3
"""Ubuntu host entry. Delegates movement/stop ownership to the existing manager."""
import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from portable_config import default_config, project_root, read_config, runtime_env, config_digest


def command(argv, env=None, timeout=20):
    process = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
    return dict(exit_code=process.returncode, stdout=process.stdout[-16000:], stderr=process.stderr[-4000:])


def doctor(root, config):
    checks = []
    def add(name, status, detail):
        checks.append(dict(name=name, status=status, detail=detail))
    add('host_platform', 'pass' if platform.system() == 'Linux' else 'fail', platform.system())
    socket_bytes = len(str(root / 'agent/agh-home-mhs/data/daemon/agnesd.sock').encode('utf-8'))
    add('unix_socket_path_length', 'pass' if socket_bytes <= 107 else 'fail',
        dict(bytes=socket_bytes, maximum=107, note='Use a shorter extraction directory; AGH uses a local Unix socket'))
    fonts = [root / 'assets/layout/NotoSansCJK-Regular.ttc',
             Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
             Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    available_fonts = [font for font in fonts if font.is_file()]
    selected_font = next(iter(available_fonts), None)
    cjk_font = selected_font is not None and 'CJK' in selected_font.name
    add('layout_sign_font', 'pass' if cjk_font else 'warn' if selected_font else 'fail',
        dict(available=[str(font) for font in available_fonts],
             selected=str(selected_font) if selected_font else None,
             cjk=cjk_font,
             note='CJK renders Chinese room signs; DejaVu alone uses English signs. No available font prevents scene startup'))
    try:
        release = Path('/etc/os-release').read_text()
        add('ubuntu_22_04', 'pass' if 'ID=ubuntu' in release and 'VERSION_ID="22.04"' in release else 'warn', release[:800])
    except OSError:
        add('ubuntu_22_04', 'fail', 'Linux Ubuntu 22.04 is the supported reproduction host')
    try:
        env = runtime_env(root, config)
        add('isaac_home', 'pass', 'Configured installation contains python.sh; no simulator started')
        isaac = Path(env['ISAAC_HOME'])
        versions = {p.name: p.read_text(errors='replace')[:600] for p in [isaac / 'VERSION', isaac / 'VERSION.txt', isaac / 'version.txt'] if p.is_file()}
        add('isaac_version', 'pass' if any('6.0.1' in s for s in versions.values()) else 'warn', versions or 'Verify the installed Isaac Sim 6.0.1 version using the official launcher/version file')
    except (OSError, ValueError) as error:
        env = os.environ.copy()
        add('isaac_home', 'fail', str(error))
    for executable in ['python3', 'node', 'corepack', 'git', 'docker']:
        add('executable_' + executable, 'pass' if shutil.which(executable) else 'fail', shutil.which(executable))
    if shutil.which('node'):
        result = command(['node', '--version'])
        add('node_24', 'pass' if result['stdout'].strip().startswith('v24.') else 'fail', result)
    if shutil.which('docker'):
        result = command(['docker', 'info', '--format', '{{json .ServerVersion}}'])
        add('docker_access', 'pass' if result['exit_code'] == 0 else 'fail', result)
        result = command(['docker', 'image', 'inspect', config['image'], '--format', '{{json .Id}}'])
        add('navigation_image', 'pass' if result['exit_code'] == 0 else 'fail', result)
    if shutil.which('nvidia-smi'):
        result = command(['nvidia-smi', '--query-gpu=index,name,driver_version,memory.total', '--format=csv,noheader'])
        indexes = [line.split(',')[0].strip() for line in result['stdout'].splitlines()]
        add('configured_gpu_present', 'pass' if str(config['gpu_index']) in indexes else 'fail', result)
    else:
        add('configured_gpu_present', 'fail', 'nvidia-smi unavailable; compatible NVIDIA GPU and driver are required')
    ports = {}
    for name, port in config['ports'].items():
        with socket.socket() as probe:
            ports[name] = dict(port=port, free=probe.connect_ex(('127.0.0.1', port)) != 0)
    add('ports_available_before_start', 'pass' if all(p['free'] for p in ports.values()) else 'warn', ports)
    marker = root / 'portable/source-patch-manifest.json'
    applied = json.loads(marker.read_text()) if marker.is_file() else None
    add('configured_source_patch', 'pass' if applied and applied.get('configuration_sha256') == config_digest(config) else 'fail', 'Run portable/control.py prepare before build')
    for item in ['agent/agh-runtime-smart-ward145/agnes.mjs', 'agent/agh-runtime-smart-ward145/daemon.mjs', 'mhs/dist/workbench.mjs', 'agent/mhs-venv/bin/python']:
        add('built_' + item, 'pass' if (root / item).is_file() else 'fail', item)
    profile = root / 'agent/agh-home-mhs/profiles/local-dev/profile.yaml'
    add('project_profile', 'pass' if profile.is_file() else 'fail',
        'The project plugin profile is distinct from account/model authentication; no account files are read')
    add('account_model_authentication', 'not_executed',
        'No credentials are read and no model is called. An existing project profile does not verify account configuration')
    add('real_agent_delivery', 'not_executed',
        'Installation/simulation readiness does not count as real Agnes delivery verification')
    assets = root / 'assets/people-v2/manifest.json'
    if assets.is_file():
        try:
            sys.path.insert(0, str(root))
            from mhs.asset_cache import verify_people_assets
            add('people_asset_integrity', 'pass', verify_people_assets(root))
        except Exception as error:
            add('people_asset_integrity', 'fail', str(error))
    else:
        add('people_asset_integrity', 'fail', 'Download the official character assets before startup')
    report = dict(schema_version=1, checked_wall=time.time(), root=str(root), instance_id=config['instance_id'],
                  checks=checks, passed=all(c['status'] != 'fail' for c in checks),
                  scope='read-only host dependency checks; no GPU simulation or model request launched',
                  verification_stage='installation', account_verified=False, real_agent_verified=False)
    output = root / 'portable/evidence'; output.mkdir(parents=True, exist_ok=True)
    (output / ('doctor-' + str(time.time_ns()) + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['init', 'prepare', 'doctor', 'start', 'status', 'stop', 'reset', 'env'])
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    root = project_root()
    path = args.config or root / 'portable/config.json'
    if args.action == 'init':
        if path.exists(): raise RuntimeError('config_exists_not_overwritten')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default_config(root), indent=2) + '\n')
        print('Created ' + str(path) + '; set ISAAC_HOME or isaac_home before startup.')
        return
    config = read_config(root, path)
    if args.action == 'prepare':
        from apply_portability import apply
        baseline = Path(__file__).with_name('baseline-sha256.json')
        expected = json.loads(baseline.read_text()) if baseline.exists() else {}
        print(json.dumps(apply(root, config, expected), ensure_ascii=False, indent=2))
        return
    if args.action == 'doctor':
        result = doctor(root, config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result['passed'] else 2)
    env = runtime_env(root, config, check_install=args.action not in {'stop', 'status'})
    if len(str(root / 'agent/agh-home-mhs/data/daemon/agnesd.sock').encode('utf-8')) > 107:
        raise RuntimeError('extraction_path_too_long_for_AGH_unix_socket_use_a_shorter_directory')
    for directory in ['logs/mhs', 'runs', 'evidence']:
        (root / directory).mkdir(parents=True, exist_ok=True)
    if args.action == 'env':
        names = ['ISAAC_HOME', 'HOSPITAL_ROOT', 'HOSPITAL_INSTANCE_ID', 'HOSPITAL_GPU_INDEX', 'HOSPITAL_ROS_DOMAIN_ID', 'HOSPITAL_IMAGE', 'HOSPITAL_MODE', 'AGH_HOME']
        print(json.dumps({name: env[name] for name in names}, indent=2))
        return
    marker = root / 'portable/source-patch-manifest.json'
    if not marker.is_file():
        raise RuntimeError('run_prepare_before_start_build_or_lifecycle')
    if args.action not in {'stop', 'status'} and json.loads(marker.read_text()).get('configuration_sha256') != config_digest(config):
        raise RuntimeError('run_prepare_before_start_build_or_lifecycle')
    if args.action == 'start':
        occupied = []
        for name, port in config['ports'].items():
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', port)) == 0:
                    occupied.append(name)
        if occupied:
            os.environ.update(env)
            sys.path.insert(0, str(root))
            from mhs import manage
            own = manage.container()
            if not (own and own['State']['Running'] and all(manage.process(name) for name in ['web', 'daemon', 'workbench'])):
                raise RuntimeError('configured_ports_busy_start_refused_stop_only_your_instance_or_choose_new_ports:' + ','.join(occupied))
    # Inactive config changes are refused above. mhs.manage retains PID/argv/cwd,
    # mount/label ownership, physical-stop proof and budget/watch safeguards.
    sys.exit(subprocess.call([sys.executable, '-m', 'mhs.manage', args.action], cwd=root, env=env))


if __name__ == '__main__':
    main()
