"""Build the complete pinned AGH/MHS distribution from source in a new project.

This entry never starts a model, simulation, navigation or public service. It
does not copy a credential home or reuse an existing compiled runtime. Linux is
the supported reproduction platform. All writable paths are beneath --root.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
COMMIT = '57d0c34b33527294042b7ab8a60d7c7c934e932e'
TREE = 'e44494fd9b523c3a9141a453856504621d89564d'
BUNDLE_SHA = '5a178d419bba25977ee5d3cc5a654b473e9d9bbca1fd387f29040182e0594c07'
SDK_SCHEMA_SHA = '592628fc4e8cadf99a6558266189ea65def23de59984c5753f1ae812c4e7564c'
URL = 'https://github.com/AgnesAI-Labs/agnes-harness.git'
NODE = 'v24.14.0'
PNPM = '10.34.5'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def contained(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('output_path_escapes_project:' + relative)
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise RuntimeError('output_symlink_refused:' + relative)
        cursor = cursor.parent
    return path


def execute(argv, cwd, env, evidence, phase):
    start = time.time()
    log = evidence / (phase + '.log')
    with log.open('xb') as stream:
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, check=False)
    record = {'phase': phase, 'argv': argv, 'cwd': str(cwd),
              'started_wall': start, 'ended_wall': time.time(),
              'returncode': result.returncode, 'log_sha256': sha(log),
              'log': log.name}
    with (evidence / 'commands.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record) + '\n')
    print(json.dumps({'phase': phase, 'returncode': result.returncode}), flush=True)
    if result.returncode:
        raise RuntimeError('command_failed:' + phase + '; see ' + str(log))


def output(argv, cwd, env):
    return subprocess.check_output(argv, cwd=cwd, env=env, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path,
                        help='New extracted project root, any absolute location.')
    parser.add_argument('--phase', choices=['fetch', 'patch', 'build', 'sdk', 'all'], default='all')
    parser.add_argument('--hub-url', help='Defaults to portable/config.json hub port, otherwise 4192.')
    parser.add_argument('--workbench-port', type=int,
                        help='Defaults to portable/config.json workbench port, otherwise 4194.')
    parser.add_argument('--upstream-bundle', type=Path,
                        help='Optional complete official Git bundle inside the project; never a compiled runtime.')
    parser.add_argument('--venv-bootstrap', action='store_true',
                        help='Use the project-local fixed virtualenv bootstrap without changing OS packages.')
    args = parser.parse_args()
    if os.name != 'posix':
        raise RuntimeError('supported_reproduction_target_is_linux')
    root = args.root.resolve(strict=True)
    portable_path = root / 'portable/config.json'
    ports = json.loads(portable_path.read_text()).get('ports', {}) if portable_path.exists() else {}
    workbench_port = args.workbench_port or ports.get('workbench', 4194)
    if type(workbench_port) is not int or not 1024 <= workbench_port <= 65535:
        raise RuntimeError('workbench_port_out_of_range')
    hub_url = args.hub_url or ('ws://127.0.0.1:' + str(ports.get('hub', 4192)) + '/ws/hub')
    if not (root / 'mhs' / 'policy' / 'index.js').is_file():
        raise RuntimeError('not_an_extracted_hospital_project')
    source = contained(root, 'agent/agh-source-mhs')
    runtime = contained(root, 'agent/agh-runtime-smart-ward145')
    sdk = contained(root, 'agent/mhs-venv')
    proof = contained(root, 'runs/reproduction/build-' + uuid.uuid4().hex)
    proof.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    env.pop('NODE_OPTIONS', None)
    # A short project-owned temporary root avoids tsx Unix socket limits.
    temporary = contained(root, '.bt')
    temporary.mkdir(exist_ok=True)
    if len(str(temporary / ('tsx-' + str(os.getuid())) / '2147483647.pipe').encode()) > 107:
        raise RuntimeError('project_path_too_long_for_unix_build_socket')
    env['TMPDIR'] = str(temporary)
    report = {'schema': 'smart-hospital-portable-runtime-build-v1', 'status': 'running',
              'commit': COMMIT, 'upstream_url': URL, 'root': str(root),
              'phase': args.phase, 'started_wall': time.time(),
              'credential_home_copied': False, 'services_started': False,
              'compiled_runtime_reused': False}
    try:
        manifest = json.loads((HERE / 'source-patch-provenance.json').read_text(encoding='utf-8-sig'))
        rows = {row['path']: row for row in manifest['files']}
        patch = HERE / 'patches' / 'hospital-smart-ward.patch'
        web_path = 'packages/web/src/app.ts'
        base_web = (HERE / 'patches/web-app.ts').read_bytes()
        if hashlib.sha256(base_web).hexdigest() != rows[web_path]['deployed_sha256']:
            raise RuntimeError('deployed_web_patch_reference_mismatch')
        anchor = "['http://127.0.0.1:4193','http://127.0.0.1:4194']"
        web_text = base_web.decode('utf-8')
        if web_text.count(anchor) != 1:
            raise RuntimeError('bound_web_origin_anchor_not_unique')
        # One precise allowed parent origin. Historical-session restrictions
        # stay byte-identical; only the configured workbench origin changes.
        configured_web = web_text.replace(anchor, "['http://127.0.0.1:" + str(workbench_port) + "']").encode('utf-8')
        configured_web_sha = hashlib.sha256(configured_web).hexdigest()
        worker_path = 'packages/worker-runtime/src/main.ts'
        base_worker = (HERE / 'patches/worker-main.ts').read_bytes()
        if hashlib.sha256(base_worker).hexdigest() != rows[worker_path]['deployed_sha256']:
            raise RuntimeError('deployed_worker_patch_reference_mismatch')
        worker_text = base_worker.decode('utf-8')
        worker_anchor = "const projectRoot = '/mnt/sda/omni-yezh/hospital-delivery-agent'"
        if worker_text.count(worker_anchor) != 1:
            raise RuntimeError('bounded_worker_root_anchor_not_unique')
        configured_worker = worker_text.replace(worker_anchor,
            "const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')").encode('utf-8')
        configured_worker_sha = hashlib.sha256(configured_worker).hexdigest()
        report['source_derivation'] = {web_path: {'deployed_sha256': rows[web_path]['deployed_sha256'],
                                                'configured_sha256': configured_web_sha,
                                                'workbench_port': workbench_port,
                                                'scope': 'replace only parent-origin allowlist; keep session edit checks'},
                                      worker_path: {'deployed_sha256': rows[worker_path]['deployed_sha256'],
                                                    'configured_sha256': configured_worker_sha,
                                                    'scope': 'derive project root from packaged executable; preserve env/profile/home/version checks'}}
        bundled = args.upstream_bundle or contained(root, 'vendor/agnes-harness.bundle')
        if bundled.exists():
            bundled = bundled.resolve(strict=True)
            if not bundled.is_relative_to(root) or not bundled.is_file() or sha(bundled) != BUNDLE_SHA:
                raise RuntimeError('upstream_bundle_identity_mismatch')
            report['upstream_bundle'] = {'path': bundled.relative_to(root).as_posix(),
                                         'sha256': sha(bundled), 'bytes': bundled.stat().st_size,
                                         'shallow_boundary': COMMIT,
                                         'source_kind': 'complete official Git source tree; no dirty checkout or compiled runtime'}
        if args.phase in ('fetch', 'all'):
            if source.exists():
                raise RuntimeError('source_output_already_exists; preserve it and choose a new project root')
            source.parent.mkdir(parents=True, exist_ok=True)
            if bundled.exists():
                # This fixed source archive was exported from an official shallow
                # checkout. Register its exact boundary before fetching; clone
                # without this boundary rejects the intentionally absent parent.
                execute(['git', 'init', str(source)], root, env, proof, 'git_init')
                with (source / '.git/shallow').open('x', encoding='ascii') as stream:
                    stream.write(COMMIT + '\n')
                execute(['git', 'fetch', str(bundled), 'HEAD'], source, env, proof, 'bundle_fetch')
            else:
                execute(['git', 'clone', '--no-checkout', URL, str(source)], root, env, proof, 'clone')
            execute(['git', 'checkout', '--detach', COMMIT], source, env, proof, 'checkout')
        if args.phase in ('fetch', 'patch', 'all'):
            if output(['git', 'rev-parse', 'HEAD'], source, env) != COMMIT:
                raise RuntimeError('official_commit_mismatch_before_patch')
            if output(['git', 'rev-parse', 'HEAD^{tree}'], source, env) != TREE:
                raise RuntimeError('official_full_source_tree_mismatch')
            if output(['git', 'status', '--porcelain=v1', '--untracked-files=no'], source, env):
                raise RuntimeError('only_clean_fixed_upstream_can_be_patched')
            for name, row in rows.items():
                if sha(source / name) != row['upstream_sha256']:
                    raise RuntimeError('pinned_upstream_file_mismatch:' + name)
            execute(['git', 'apply', '--check', str(patch)], source, env, proof, 'patch_check')
            execute(['git', 'apply', str(patch)], source, env, proof, 'patch_apply')
            for name, row in rows.items():
                if name == 'packages/mhs/plugin/client/agnes.client.json':
                    continue
                if sha(source / name) != row['deployed_sha256']:
                    raise RuntimeError('patched_file_mismatch:' + name)
            (source / web_path).write_bytes(configured_web)
            (source / worker_path).write_bytes(configured_worker)
            client = source / 'packages/mhs/plugin/client/agnes.client.json'
            descriptor = json.loads(client.read_text())
            descriptor['client']['publicConfig']['hubUrl'] = hub_url
            client.write_text(json.dumps(descriptor, indent=2) + '\n')
        if not source.is_dir() or output(['git', 'rev-parse', 'HEAD'], source, env) != COMMIT:
            raise RuntimeError('complete_pinned_checkout_required')
        if output(['git', 'rev-parse', 'HEAD^{tree}'], source, env) != TREE:
            raise RuntimeError('official_full_source_tree_mismatch')
        expected_dirty = {name for name in rows if rows[name]['upstream_sha256'] != rows[name]['deployed_sha256']}
        dirty = output(['git', 'diff', '--name-only', 'HEAD'], source, env).splitlines()
        if set(dirty) != expected_dirty:
            raise RuntimeError('unexpected_missing_or_extra_source_changes')
        status = subprocess.check_output(['git', 'status', '--porcelain=v1', '--untracked-files=all'],
                                         cwd=source, env=env, text=True).splitlines()
        if len(status) != len(expected_dirty) or set(status) != {' M ' + name for name in expected_dirty}:
            raise RuntimeError('unexpected_staged_renamed_or_untracked_source_change')
        for name, row in rows.items():
            if name == 'packages/mhs/plugin/client/agnes.client.json':
                continue
            expected_sha = (configured_web_sha if name == web_path else
                            configured_worker_sha if name == worker_path else row['deployed_sha256'])
            if sha(source / name) != expected_sha:
                raise RuntimeError('patched_source_changed:' + name)
        if json.loads((source / 'packages/mhs/plugin/client/agnes.client.json').read_text())['client']['publicConfig']['hubUrl'] != hub_url:
            raise RuntimeError('mhs_client_hub_url_mismatch')
        report['source_sha256'] = {name: sha(source / name) for name in rows}
        report['patch_sha256'] = sha(patch)
        if args.phase in ('build', 'all'):
            if output(['node', '--version'], source, env) != NODE:
                raise RuntimeError('node_version_must_be_' + NODE)
            # Corepack selects the fixed manager from upstream package.json.
            # Checking in another cwd would inspect Corepack's unrelated default.
            if output(['corepack', 'pnpm', '--version'], source, env) != PNPM:
                raise RuntimeError('pnpm_version_must_be_' + PNPM)
            corepack = shutil.which('corepack', path=env.get('PATH'))
            if not corepack:
                raise RuntimeError('corepack_not_found')
            build_bin = contained(root, 'agent/build-bin')
            build_bin.mkdir(parents=True, exist_ok=True)
            shim = build_bin / 'pnpm'
            shim_source = '#!/bin/sh\nexec ' + shlex.quote(corepack) + ' pnpm "$@"\n'
            if shim.exists() and shim.read_text() != shim_source:
                raise RuntimeError('unknown_existing_project_pnpm_shim')
            if not shim.exists():
                shim.write_text(shim_source)
                shim.chmod(0o755)
            env['PATH'] = str(build_bin) + os.pathsep + env.get('PATH', '')
            if runtime.exists():
                raise RuntimeError('runtime_output_already_exists; never overwrite a runtime')
            if not shutil.which('cc'):
                raise RuntimeError('install_C_compiler_and_matching_Node_headers')
            execute(['corepack', 'pnpm', 'install', '--frozen-lockfile'], source, env, proof, 'pnpm_install')
            execute(['corepack', 'pnpm', '--filter', '@agnes/cli', 'build:local', '--output-dir', str(runtime)],
                    source, env, proof, 'complete_agh_build')
            execute(['corepack', 'pnpm', '--filter', '@agnes/mhs', 'build:plugin'],
                    source, env, proof, 'mhs_plugin_build')
            dist = contained(root, 'mhs/dist')
            dist.mkdir(exist_ok=True)
            policy_dist = contained(root, 'mhs/policy/dist')
            policy_dist.mkdir(exist_ok=True)
            script = """const {createRequire}=require('node:module');
const path=require('node:path');const root=process.argv[1];
const source=path.join(root,'agent/agh-source-mhs');
const req=createRequire(path.join(source,'packages/base/package.json'));
for(const [entry,out] of [['mhs/workbench.js','mhs/dist/workbench.mjs'],['mhs/policy/index.js','mhs/policy/dist/index.mjs']]){
req('esbuild').buildSync({entryPoints:[path.join(root,entry)],outfile:path.join(root,out),bundle:true,platform:'node',format:'esm',target:'node24',nodePaths:[path.join(source,'packages/base/node_modules')],banner:{js:"import{createRequire as __cr}from'node:module';const require=__cr(import.meta.url);"}});
}"""
            execute(['node', '-e', script, str(root)], root, env, proof, 'hospital_policy_workbench_build')
            execute(['node', str(runtime / 'agnes.mjs'), '--help'], root, env, proof, 'cli_help')
            report['runtime_sha256'] = {path.relative_to(runtime).as_posix(): sha(path)
                                       for path in sorted(runtime.rglob('*')) if path.is_file() and not path.is_symlink()}
        if args.phase in ('sdk', 'all'):
            if sdk.exists():
                raise RuntimeError('python_venv_already_exists; never reuse an unknown environment')
            bootstrap_needed = args.venv_bootstrap
            if not bootstrap_needed:
                try:
                    execute([sys.executable, '-m', 'venv', str(sdk)], root, env, proof, 'venv')
                    report['venv_mode'] = 'stdlib_venv'
                except RuntimeError as error:
                    if not str(error).startswith('command_failed:venv;'):
                        raise
                    # Only preserve the new partial output created by this
                    # invocation. An existing output was refused before spawn.
                    if sdk.exists():
                        if sdk.is_symlink() or not sdk.is_dir() or not sdk.resolve().is_relative_to(root):
                            raise RuntimeError('partial_new_venv_not_owned_regular_directory')
                        preserved = proof / 'failed-stdlib-venv'
                        sdk.rename(preserved)
                        report['stdlib_venv_failure_preserved'] = preserved.relative_to(root).as_posix()
                    report['stdlib_venv_error'] = str(error)
                    bootstrap_needed = True
            if bootstrap_needed:
                bootstrap = contained(root, 'agent/venv-bootstrap')
                if bootstrap.exists():
                    raise RuntimeError('bootstrap_output_already_exists; never reuse an unknown bootstrap')
                execute([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', '--target', str(bootstrap),
                         '--requirement', str(HERE / 'bootstrap-requirements.lock.txt')],
                        root, env, proof, 'project_virtualenv_bootstrap_install')
                seed_env = dict(env, PYTHONPATH=str(bootstrap))
                execute([sys.executable, '-m', 'virtualenv', '--app-data',
                         str(contained(root, 'agent/virtualenv-cache')), str(sdk)],
                        root, seed_env, proof, 'project_virtualenv_create')
                report['venv_mode'] = 'project_local_virtualenv_21.14.6'
            python = str(sdk / 'bin/python')
            sdk_env = dict(env, PATH=str(sdk / 'bin') + os.pathsep + env.get('PATH', ''))
            execute([python, '-m', 'pip', 'install', '--no-cache-dir', '--requirement', str(HERE / 'requirements.lock.txt')],
                    root, sdk_env, proof, 'python_locked_dependencies')
            sdk_schema = source / 'packages/mhs/schema/mhs-v1.json'
            if sha(sdk_schema) != SDK_SCHEMA_SHA:
                raise RuntimeError('fixed_sdk_schema_sha_mismatch')
            # The fixed SDK imports the schema from its source parent rather
            # than embedding it in its wheel. Editable installation preserves
            # that official layout; it is not a copied runtime or altered SDK.
            execute([python, '-m', 'pip', 'install', '--no-cache-dir', '--no-build-isolation', '--no-deps', '--editable',
                     str(source / 'packages/mhs/python')], root, sdk_env, proof, 'python_sdk_from_pinned_source')
            execute([python, '-m', 'pip', 'check'], root, sdk_env, proof, 'python_dependency_check')
            execute([python, '-c', 'import agnes_mhs,mcp,pydantic,jsonschema,websockets; print("sdk_imports_ok")'],
                    root, sdk_env, proof, 'python_sdk_import')
            report['python_freeze'] = output([python, '-m', 'pip', 'freeze'], root, sdk_env).replace(str(root), '${PROJECT_ROOT}').splitlines()
            report['python_sdk_commit'] = COMMIT
            report['python_sdk_installation'] = 'editable official fixed source; schema remains in fixed source tree'
            report['python_sdk_schema'] = {'path': sdk_schema.relative_to(root).as_posix(),
                                           'sha256': SDK_SCHEMA_SHA}
            report['sdk_transport'] = 'JPEG only; no PyAV dependency requested'
        report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error=type(error).__name__ + ':' + str(error))
        raise
    finally:
        report['ended_wall'] = time.time()
        (proof / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print('BUILD_REPORT ' + str(proof / 'report.json'), flush=True)


if __name__ == '__main__':
    main()
