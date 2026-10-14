"""Generate a relocatable source copy with exact baseline checks before builds.

Run this only in a new extracted reproduction directory, before AGH build/setup.
Original experiment/evidence files are not processed. Different port/domain
settings require a fresh extraction. ISAAC_HOME and GPU are runtime settings.
"""
import argparse
import difflib
import hashlib
import json
import re
from pathlib import Path
from portable_config import config_digest, project_root, read_config

OLD_ROOT = '/mnt/sda/omni-yezh/hospital-delivery-agent'
SUFFIXES = {'.py', '.js', '.mjs', '.sh', '.json', '.yaml', '.html'}
STARTUP_REVISION = 'coldstart-wait-v1'
STARTUP_LIMITS = {
    'lifecycle_manager_response_wall_seconds': 150,
    'navigation_readiness_wall_seconds': 480,
    'derivation': '60 discovery + 10 warmup + 2*150 manager replies + 9*5 state replies = 415; readiness adds 65 seconds margin',
    'unchanged': {'simulation_first_frame_wall_seconds': 900,
                  'agent_model_requests': 60, 'agent_sim_seconds': 2400,
                  'agent_wall_seconds': 3600,
                  'navigation_qualification_arrival_and_stop_gates': True},
}


def startup_adjustment(relative, text):
    """Two exact cold-start waits only; never changes motion or task budgets."""
    anchors = {
        'scripts/nav_bootstrap.py': ('reply=wait_result(client.call_async(req),45)',
                                     'reply=wait_result(client.call_async(req),150)'),
        'mhs/manage.py': ('def wait_ready():\n    end=time.monotonic()+180',
                          'def wait_ready():\n    end=time.monotonic()+480'),
    }
    if relative not in anchors:
        return text
    before, after = anchors[relative]
    pairs = list(dict.fromkeys([(before, after), (before.replace('\n', '\r\n'), after.replace('\n', '\r\n'))]))
    matched = [(old, new) for old, new in pairs if old in text]
    if sum(text.count(old) for old, _ in pairs) != 1 or any(new in text for _, new in pairs):
        raise RuntimeError('startup_wait_anchor_mismatch:' + relative)
    before, after = matched[0]
    return text.replace(before, after, 1)


def transform(relative, text, config):
    old = text
    # Only source directories are traversed; archived reports/manifests are excluded.
    mapping = {'4188': config['ports']['api'], '4189': config['ports']['chat'],
               '4192': config['ports']['hub'], '4193': config['ports']['workbench'],
               '4194': config['ports']['workbench'], '4195': config['ports']['daemon']}
    text = re.sub(r'\b(?:4188|4189|4192|4193|4194|4195)\b', lambda m: str(mapping[m.group()]), text)
    text = text.replace('ROS_DOMAIN_ID=73', 'ROS_DOMAIN_ID=' + str(config['ros_domain_id']))
    text = text.replace("ROS_DOMAIN_ID='73'", "ROS_DOMAIN_ID='" + str(config['ros_domain_id']) + "'")
    text = text.replace('hospital-delivery:humble-v1', config['image'])
    text = text.replace('hospital-delivery-mhs-runtime', config['instance_id'] + '-runtime')
    text = text.replace('hospital-delivery-runtime', config['instance_id'] + '-legacy')
    if relative in ['mhs/hospital_sim.py', 'sim/hospital.py', 'scripts/pull_ros.py', 'mhs/capture_scope_evidence.py']:
        text = text.replace("Path('" + OLD_ROOT + "')", 'Path(__file__).resolve().parents[1]')
    if relative in ['mhs/hospital_sim.py', 'sim/hospital.py']:
        text = text.replace('"active_gpu":1', '"active_gpu":int(os.environ.get("HOSPITAL_GPU_INDEX","0"))')
        text = text.replace('assets=get_assets_root_path()', 'assets=os.environ.get("HOSPITAL_ASSETS_ROOT") or get_assets_root_path()')
    if relative == 'scripts/manage.py':
        text = text.replace("SIM=Path('/mnt/sda/omni-yezh/isaac-sim-6.0.1')", "SIM=Path(os.environ['ISAAC_HOME']).expanduser().resolve()")
        text = text.replace("LABEL='hospital-delivery-agent'", "LABEL='" + config['instance_id'] + "-legacy'")
    if relative == 'mhs/manage.py':
        text = text.replace("LABEL='hospital-delivery-agent-mhs'", "LABEL='" + config['instance_id'] + "'")
        text = text.replace('LOG.mkdir(exist_ok=True)', 'LOG.mkdir(parents=True,exist_ok=True)')
        # UDP-only FastDDS is already configured: sharing the host IPC namespace is unnecessary.
        text = text.replace("'--ipc','host',", '')
        anchor = "if c['Config']['Labels'].get('project')!=LABEL:raise RuntimeError('container_ownership_mismatch')"
        checked = anchor + "\n    mounts=c.get('Mounts',[])\n    if not any(m.get('Destination')=='/project' and Path(m.get('Source','')).resolve()==ROOT for m in mounts):raise RuntimeError('container_mount_ownership_mismatch')"
        if text.count(anchor) != 1:
            raise RuntimeError('container_ownership_anchor_mismatch')
        text = text.replace(anchor, checked)
    if relative == 'mhs/workbench.js':
        text = text.replace("process.env.HOSPITAL_ROOT??'" + OLD_ROOT + "'", 'process.env.HOSPITAL_ROOT')
        anchor = "const smartMode=process.env.HOSPITAL_MODE==='smart-ward-v1';"
        text = text.replace(anchor, "if(!root)throw Error('HOSPITAL_ROOT_required_use_portable_control');\n" + anchor)
    if relative == 'mhs/policy/runtime.js':
        text = text.replace("export const root='" + OLD_ROOT + "';", "export const root=process.env.HOSPITAL_ROOT;\nif(!root)throw Error('HOSPITAL_ROOT_required_use_portable_control');")
    if relative == 'mhs/mcp.sh':
        text = '#!/usr/bin/env bash\nset -euo pipefail\nproject_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"\ncd -- "$project_root"\nexport HOSPITAL_ROOT="$project_root"\nexec "$project_root/agent/mhs-venv/bin/python" -m mhs.mcp_server\n'
    if relative == 'scripts/agh_client.py':
        text = text.replace("'/mnt/sda/omni-yezh/hospital-delivery-agent/agent/agh-runtime/agnes.mjs'", "str(ROOT/'agent/agh-runtime/agnes.mjs')")
    if relative == 'scripts/agh_resource.py':
        text = 'from pathlib import Path\nROOT=Path(__file__).resolve().parents[1]\n' + text
        text = text.replace('"' + OLD_ROOT + '/agent/agh-home"', 'str(ROOT/"agent/agh-home")')
        text = text.replace('"' + OLD_ROOT + '/scripts/mcp.sh"', 'str(ROOT/"scripts/mcp.sh")')
        text = text.replace('"' + OLD_ROOT + '/agent/agh-runtime/agnes.mjs"', 'str(ROOT/"agent/agh-runtime/agnes.mjs")')
    if relative == 'configs/mcp-definition.json':
        text = text.replace(OLD_ROOT + '/scripts/mcp.sh', './scripts/mcp.sh')
    # This is an obsolete source-generation utility, not a runtime entry point;
    # make its generated log location relative as well.
    if relative == 'mhs/prepare_v2.py':
        text = text.replace(OLD_ROOT + '/runs/mhs-v2/policy-errors.jsonl', 'runs/mhs-v2/policy-errors.jsonl')
    if relative == 'mhs/build_runtime.py':
        text = text.replace('/usr/bin/corepack pnpm', 'corepack pnpm')
    if relative in ['mhs/policy/test-turn-stopping-abi172.mjs', 'mhs/policy/test-session-scope.mjs']:
        # The paths here are mocked test fixtures, never runtime/account locations.
        text = text.replace(OLD_ROOT, '/tmp/hospital-portability-test')
    text = startup_adjustment(relative, text)
    return text if text != old else None


def candidates(root):
    return sorted(p for directory in ['mhs', 'scripts', 'sim', 'service', 'configs', 'launch']
                  for p in (root / directory).rglob('*')
                  if p.is_file() and p.suffix in SUFFIXES
                  and 'dist' not in p.relative_to(root).parts and '__pycache__' not in p.parts)


def apply(root, config, expected=None):
    root = Path(root).resolve()
    marker = root / 'portable/source-patch-manifest.json'
    digest = config_digest(config)
    if marker.exists():
        prior = json.loads(marker.read_text())
        if prior['configuration_sha256'] != digest:
            raise RuntimeError('configuration_changed_extract_a_fresh_source_copy_before_prepare')
        for row in prior['files']:
            if hashlib.sha256((root / row['path']).read_bytes()).hexdigest() != row['after_sha256']:
                raise RuntimeError('patched_source_changed:' + row['path'])
        if prior.get('startup_revision') != STARTUP_REVISION or prior.get('startup_limits') != STARTUP_LIMITS:
            raise RuntimeError('prepared_source_requires_explicit_startup_wait_migration')
        return dict(prior, idempotent=True)
    expected = expected or {}
    changes = []
    diffs = []
    for path in candidates(root):
        relative = path.relative_to(root).as_posix()
        original = path.read_bytes()
        before = hashlib.sha256(original).hexdigest()
        text = original.decode('utf-8')
        updated = transform(relative, text, config)
        if updated is None:
            continue
        if expected and expected.get(relative) != before:
            raise RuntimeError('baseline_sha256_mismatch:' + relative)
        if path.suffix == '.py':
            compile(updated, relative, 'exec')
        result = updated.encode('utf-8')
        changes.append((path, result, dict(path=relative, before_sha256=before,
                                        after_sha256=hashlib.sha256(result).hexdigest())))
        diffs.extend(difflib.unified_diff(text.splitlines(True), updated.splitlines(True),
                                        fromfile='a/' + relative, tofile='b/' + relative))
    if not changes:
        raise RuntimeError('no_portability_changes_found_wrong_input_source')
    # Prepare is a source-generation step and must precede any runtime start.
    for pidfile in (root / 'logs').rglob('*.pid'):
        try:
            pid = int(pidfile.read_text().strip())
            if (Path('/proc') / str(pid)).exists():
                raise RuntimeError('prepare_requires_an_inactive_fresh_extraction:' + pidfile.name)
        except ValueError:
            raise RuntimeError('invalid_runtime_marker_in_fresh_extraction')
    for path, result, _ in changes:
        path.write_bytes(result)
        if path.suffix == '.sh':
            path.chmod(0o755)
    marker.parent.mkdir(parents=True, exist_ok=True)
    manifest = dict(schema_version=1, configuration_sha256=digest, files=[row for _, _, row in changes],
                    startup_revision=STARTUP_REVISION, startup_limits=STARTUP_LIMITS,
                    scope='source-only relocatable root, instance ports/domain/container, GPU/install settings',
                    untouched='account data, historical evidence, business/guard algorithms and budgets')
    marker.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (marker.parent / 'portability.patch').write_text(''.join(diffs), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=project_root())
    parser.add_argument('--config', type=Path)
    parser.add_argument('--expected', type=Path)
    args = parser.parse_args()
    expected_path = args.expected or Path(__file__).with_name('baseline-sha256.json')
    expected = json.loads(expected_path.read_text()) if expected_path.exists() else {}
    result = apply(args.root, read_config(args.root, args.config), expected)
    print(json.dumps(result, ensure_ascii=False, indent=2))
