"""Use AGH's ordinary inspect/add/trust/enable flow in an isolated new home.

Configure your own model account with `agnes.mjs config` or Provider settings
before real Agent acceptance. It never imports an account, reads secret contents,
starts a robot, relaxes another account's approvals, or trusts unseen packages.
"""
import argparse
import json
import os
from pathlib import Path
import pty
import re
import select
import socket
import sqlite3
import subprocess
import sys
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--home', type=Path, help='Defaults to ROOT/agent/agh-home-mhs')
    parser.add_argument('--profile', default='local-dev')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute or os.name != 'posix':
        raise RuntimeError('explicit_linux_setup_required')
    root = args.root.resolve(strict=True)
    home = (args.home or root / 'agent/agh-home-mhs').resolve()
    if not home.is_relative_to(root) or home.is_symlink():
        raise RuntimeError('new_home_must_belong_to_new_project')
    if home != root / 'agent/agh-home-mhs' or args.profile != 'local-dev':
        raise RuntimeError('project_policy_requires_its_own_canonical_home_and_local_dev_profile')
    profile = home / ('profiles/' + args.profile + '/profile.yaml')
    if not profile.exists():
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(json.dumps({'name': args.profile,
                                      'presets': {'default': 'standard', 'allowed': ['standard']},
                                      'packages': []}, indent=2) + '\n')
    runtime = root / 'agent/agh-runtime-smart-ward145/agnes.mjs'
    if not runtime.is_file():
        raise RuntimeError('build_complete_pinned_runtime_first')
    # Fixed official CLI package commands read the instance from environment;
    # --home/--profile before the command are not global CLI options.
    cli = ['node', str(runtime)]
    # Use this extracted project's validated, nonsecret deployment config. A
    # clean SSH shell need not already define ISAAC_HOME or any project ports.
    sys.path.insert(0, str(root / 'portable'))
    from portable_config import read_config, runtime_env
    config = read_config(root)
    ports = config['ports']
    env = runtime_env(root, config)
    env['HOSPITAL_INFER_ADMISSION'] = 'smart-ward-v1.89'
    evidence = root / ('runs/reproduction/plugins-' + uuid.uuid4().hex)
    evidence.mkdir(parents=True, exist_ok=False)
    report = {'schema': 'smart-hospital-portable-plugin-install-v1', 'status': 'running',
              'started_wall': time.time(), 'project_root': str(root), 'home': str(home),
              'account_source': 'not imported or copied; configure own account for real Agent use',
              'real_agent_acceptance': 'NOT_EXECUTED_BY_PLUGIN_INSTALL', 'phase': 'prepare'}

    def call(argv, name):
        report['phase'] = name
        sources = {'file:./agent/agh-source-mhs/packages/mhs/plugin', 'file:./mhs/policy'}
        identities = {'@agnes/mhs', 'hospital-mhs-policy'}
        confirm = ((argv[:2] == ['package', 'add'] and argv[2] in sources)
                   or (argv[:2] in [['package', 'trust'], ['package', 'enable']] and argv[2] in identities)
                   or (argv[:3] == ['mcp', 'add', 'hospital-business'] and '--stdio' in argv
                       and argv[argv.index('--stdio') + 1] == str(root / 'mhs/mcp.sh'))
                   or (argv[:3] in [['mcp', 'trust', 'hospital-business'], ['mcp', 'test', 'hospital-business']]
                       and '--expected-revision' in argv))
        master, slave = pty.openpty()
        process = subprocess.Popen(cli + argv, cwd=root, env=env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        chunks = []
        answered = False
        started = time.monotonic()
        try:
            while time.monotonic() - started < 120:
                if select.select([master], [], [], .2)[0]:
                    try:
                        chunk = os.read(master, 65536).decode(errors='replace')
                    except OSError:
                        break
                    chunks.append(chunk)
                    print(chunk, end='', flush=True)
                    if confirm and '[y/N]' in ''.join(chunks) and not answered:
                        os.write(master, b'y\n')
                        answered = True
                if process.poll() is not None:
                    break
            if process.poll() is None:
                process.terminate()
            code = process.wait(timeout=10)
        finally:
            os.close(master)
        text = ''.join(chunks)
        (evidence / (name + '.log')).write_text(text)
        if code:
            raise RuntimeError('package_or_mcp_setup_failed:' + name)
        return text

    def owned_daemon(discovery):
        pid = discovery.get('owner', {}).get('pid')
        if type(pid) is not int or pid <= 1:
            raise RuntimeError('daemon_discovery_owner_missing')
        proc = Path('/proc') / str(pid)
        argv = [part.decode() for part in (proc / 'cmdline').read_bytes().split(b'\0') if part]
        cwd = (proc / 'cwd').resolve()
        daemon_path = str(root / 'agent/agh-runtime-smart-ward145/daemon.mjs')

        def option(name):
            indexes = [index for index, value in enumerate(argv) if value == name]
            if len(indexes) > 1 or indexes and indexes[0] + 1 >= len(argv):
                raise RuntimeError('daemon_identity_option_invalid')
            return argv[indexes[0] + 1] if indexes else None

        home_arg = option('--home')
        data_arg = option('--data-dir')
        direct_home = home_arg == str(home)
        # Official package-command autostart passes --data-dir and inherits
        # AGH_HOME instead of emitting --home. Match its complete instance
        # identity, never just its cwd or any process named daemon.mjs.
        automatic_home = (home_arg is None and data_arg == str(home / 'data')
                          and discovery.get('dataDir') == str(home / 'data'))
        if (cwd != root or len(argv) < 2 or argv[1] != daemon_path
                or option('--profile') != args.profile or option('--workspace') != str(root)
                or not (direct_home or automatic_home)):
            raise RuntimeError('daemon_process_ownership_mismatch')
        return {'pid': pid, 'cwd': str(cwd), 'daemon_path': daemon_path,
                'home_argument': home_arg, 'data_dir_argument': data_arg,
                'home_identity_source': 'explicit_home' if direct_home else 'official_data_dir_and_discovery',
                'profile_argument': option('--profile'), 'workspace_argument': option('--workspace'),
                'argv_identity_verified': True,
                'web': discovery.get('web')}

    def prepare_mode_and_daemon(force_restart=False, phase='initial'):
        # MCP's standard child transport inherits only HOME/LOGNAME/PATH/SHELL/
        # TERM/USER. Persist the legitimate mode so child and policy select the
        # same closed-world catalog; do not broaden its tool allowlist.
        mode_path = root / 'configs/runtime-profile.json'
        if mode_path.exists():
            if json.loads(mode_path.read_text()).get('mode') != 'smart-ward-v1':
                raise RuntimeError('project_mode_mismatch_refuse_to_change_existing_instance')
        else:
            mode_path.parent.mkdir(parents=True, exist_ok=True)
            with mode_path.open('x') as stream:
                stream.write(json.dumps({'mode': 'smart-ward-v1'}, indent=2) + '\n')
        (root / 'runs/smart-ward-v1').mkdir(parents=True, exist_ok=True)
        expected = 'ws://127.0.0.1:' + str(ports.get('daemon', 4195))
        discovery_path = home / 'data/daemon/discovery.json'
        if discovery_path.exists():
            current = json.loads(discovery_path.read_text())
            pid = current.get('owner', {}).get('pid')
            if current.get('ready') and type(pid) is int and (Path('/proc') / str(pid)).exists():
                identity = owned_daemon(current)
                report.setdefault('daemon_lifecycle', []).append({'phase': phase, 'before': identity})
                if force_restart or current.get('web', {}).get('url') != expected:
                    # Normal lifecycle stop, scoped to this independently owned
                    # home/workspace. No signal or stop of a public daemon.
                    call(['daemon', 'stop', '--home', str(home), '--workspace', str(root)], phase + '-stop-own-daemon')
                    deadline = time.monotonic() + 20
                    while (Path('/proc') / str(pid)).exists() and time.monotonic() < deadline:
                        stat = (Path('/proc') / str(pid) / 'stat').read_text()
                        if stat.split(') ', 1)[1].startswith('Z '):
                            break
                        time.sleep(.1)
                    if (Path('/proc') / str(pid)).exists():
                        stat = (Path('/proc') / str(pid) / 'stat').read_text()
                        if not stat.split(') ', 1)[1].startswith('Z '):
                            raise RuntimeError('own_temporary_daemon_stop_not_confirmed')
        report['phase'] = 'start-own-fixed-port-daemon'
        command = [sys.executable, '-c', 'from mhs.manage import start_daemon; start_daemon()']
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
        (evidence / (phase + '-start-own-fixed-port-daemon.log')).write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError('own_fixed_port_daemon_start_failed')
        current = json.loads(discovery_path.read_text())
        identity = owned_daemon(current)
        report.setdefault('daemon_lifecycle', []).append({'phase': phase, 'after': identity})
        if not current.get('ready') or current.get('web', {}).get('url') != expected:
            raise RuntimeError('own_fixed_port_daemon_health_not_confirmed')

    def inference_counts():
        path = home / 'data/sessions.db'
        if not path.exists():
            return {'status': 'NA', 'reason': 'sessions_ledger_not_created'}
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            return {'status': 'verified',
                    'request_sent': connection.execute("SELECT count(*) FROM events WHERE type='request/sent'").fetchone()[0],
                    'inference_intents': connection.execute("SELECT count(*) FROM events WHERE type='effect/intent' AND json_extract(data,'$.kind')='inference'").fetchone()[0],
                    'scope': 'counts only in new independent ledger; no message or request contents exported'}
        finally:
            connection.close()

    def stop_installation_daemon():
        discovery_path = home / 'data/daemon/discovery.json'
        current = json.loads(discovery_path.read_text())
        identity = owned_daemon(current)
        pid = identity['pid']
        began = time.time()
        call(['daemon', 'stop', '--home', str(home), '--workspace', str(root)], 'installation-final-stop-own-daemon')

        def process_alive():
            try:
                stat = (Path('/proc') / str(pid) / 'stat').read_text()
                return not stat.split(') ', 1)[1].startswith('Z ')
            except FileNotFoundError:
                return False

        def port_open(port):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(.25)
                return connection.connect_ex(('127.0.0.1', port)) == 0

        checked_ports = {name: ports[name] for name in ['hub', 'daemon']}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            alive = process_alive()
            listeners = {name: port_open(port) for name, port in checked_ports.items()}
            if not alive and not any(listeners.values()):
                break
            time.sleep(.1)
        proof = {'normal_stop_command': True, 'verified_identity_before_stop': identity,
                 'started_wall': began, 'ended_wall': time.time(),
                 'daemon_alive_after': process_alive(), 'checked_ports': checked_ports,
                 'listening_after': {name: port_open(port) for name, port in checked_ports.items()},
                 'installed_trust_enabled_profile_preserved': True,
                 'public_instance_touched': False}
        report['installation_final_stop'] = proof
        (evidence / 'installation-final-stop.json').write_text(json.dumps(proof, indent=2) + '\n')
        if proof['daemon_alive_after'] or any(proof['listening_after'].values()):
            raise RuntimeError('own_installation_daemon_or_hub_port_release_not_confirmed')

    try:
        report['model_counts_before'] = inference_counts()
        prepare_mode_and_daemon()
        for index, (source, identity) in enumerate([
            ('file:./agent/agh-source-mhs/packages/mhs/plugin', '@agnes/mhs'),
            ('file:./mhs/policy', 'hospital-mhs-policy')
        ]):
            prefix = 'package-' + str(index)
            call(['package', 'inspect', source], prefix + '-inspect')
            lock = home / ('profiles/' + args.profile + '/agnes-lock.json')
            packages = json.loads(lock.read_text()).get('packages', {}) if lock.exists() else {}
            if identity not in packages:
                call(['package', 'add', source], prefix + '-add')
            operations = json.loads((home / 'data/daemon/package-operations/operations.json').read_text())['operations']
            previews = [row['operation']['preview'] for row in operations
                        if row['operation'].get('preview', {}).get('id') == identity]
            if not previews or previews[-1].get('blockers'):
                raise RuntimeError('inspected_package_blocked:' + identity)
            preview = previews[-1]
            (evidence / (prefix + '-preview.json')).write_text(json.dumps(preview, indent=2) + '\n')
            call(['package', 'trust', identity, preview['integrity'], preview['capabilityHash']], prefix + '-trust')
            call(['package', 'enable', identity], prefix + '-enable')
        listing = call(['mcp', 'list'], 'mcp-list')
        current = call(['mcp', 'get', 'hospital-business'], 'mcp-get') if 'hospital-business' in listing else call([
            'mcp', 'add', 'hospital-business', '--name', 'Hospital business', '--stdio', str(root / 'mhs/mcp.sh')
        ], 'mcp-add')
        revision = re.search(r'revision=([a-f0-9]{64})', current)
        if not revision:
            raise RuntimeError('mcp_revision_missing')
        call(['mcp', 'trust', 'hospital-business', '--expected-revision', revision.group(1)], 'mcp-trust')
        current = call(['mcp', 'get', 'hospital-business'], 'mcp-current')
        revision = re.search(r'revision=([a-f0-9]{64})', current)
        if not revision:
            raise RuntimeError('mcp_revision_missing_after_trust')
        call(['mcp', 'test', 'hospital-business', '--expected-revision', revision.group(1)], 'mcp-test')
        # Keep account configuration saved by the normal AGH configuration service.
        update = """const {createRequire}=require('node:module');const fs=require('node:fs');
    const root=process.argv[1],profile=process.argv[2];
    const req=createRequire(root+'/agent/agh-source-mhs/packages/package-manager/package.json');
    const config=req('yaml').parse(fs.readFileSync(profile,'utf8'));
    if(!config||typeof config!=='object'||Array.isArray(config))throw Error('invalid_profile');
    config.presets={default:'hospital-mhs',allowed:['hospital-mhs']};
    config.packages=[{id:'@agnes/mhs',source:'file:./agent/agh-source-mhs/packages/mhs/plugin',enabled:true},{id:'hospital-mhs-policy',source:'file:./mhs/policy',enabled:true}];
    fs.writeFileSync(profile,JSON.stringify(config,null,2)+'\\n');"""
        subprocess.run(['node', '-e', update, str(root), str(profile)], cwd=root, env=env, check=True)
        # Activate this final project profile through an ordinary, verified
        # lifecycle restart once. This does not touch the public instance.
        prepare_mode_and_daemon(force_restart=True, phase='final-profile')
        call(['package', 'status'], 'package-status')
        catalog_check = """import asyncio,json,sys
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
async def check():
    root=sys.argv[1]
    async with stdio_client(StdioServerParameters(command=root+'/mhs/mcp.sh',cwd=root)) as (reader,writer):
        async with ClientSession(reader,writer) as client:
            await client.initialize()
            tools=await client.list_tools()
            print(json.dumps({'names':sorted(tool.name for tool in tools.tools),'count':len(tools.tools)}))
asyncio.run(check())"""
        sdk_env = dict(env, PATH=str(root / 'agent/mhs-venv/bin') + os.pathsep + env.get('PATH', ''))
        catalog = subprocess.run([str(root / 'agent/mhs-venv/bin/python'), '-c', catalog_check, str(root)],
                                 cwd=root, env=sdk_env, capture_output=True, text=True, timeout=45)
        (evidence / 'final-mcp-catalog.log').write_text(catalog.stdout + catalog.stderr)
        if catalog.returncode:
            raise RuntimeError('actual_final_mcp_catalog_read_failed')
        catalog = json.loads(catalog.stdout.strip().splitlines()[-1])
        expected_tools = ['get_state', 'get_route_options', 'create_specimen_delivery', 'publish_service_plan',
                          'defer_order', 'close_order', 'verify_services', 'wait_event']
        if catalog.get('count') != 8 or catalog.get('names') != sorted(expected_tools):
            raise RuntimeError('actual_final_mcp_catalog_not_exact_smart_eight_tools')
        report['actual_final_mcp_catalog'] = catalog
        report['model_counts_after'] = inference_counts()
        if report['model_counts_before'].get('status') == report['model_counts_after'].get('status') == 'verified':
            report['model_counts_delta'] = {name: report['model_counts_after'][name] - report['model_counts_before'][name]
                                             for name in ['request_sent', 'inference_intents']}
            if any(report['model_counts_delta'].values()):
                raise RuntimeError('unexpected_model_request_during_plugin_install')
        # Installation leaves only persisted enabled/trusted/profile state.
        # The ordinary controller later starts the complete new instance; it
        # keeps its strict refusal of unexplained or partial port occupation.
        stop_installation_daemon()
        report.update(status='passed', normal_install_flow=['inspect', 'add', 'trust exact integrity/capability hash', 'enable'], mcp_test='actual service transport/catalog test; not Agent task acceptance')
    except Exception as error:
        report.update(status='failed', error=type(error).__name__ + ':' + str(error))
        raise
    finally:
        report['ended_wall'] = time.time()
        (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print('PLUGIN_REPORT ' + str(evidence / 'report.json'), flush=True)



if __name__ == '__main__':
    main()
