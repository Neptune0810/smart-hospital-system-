"""One instance per unpacked directory. No account, host, SSH or proxy defaults."""
import hashlib
import json
import os
import re
from pathlib import Path

PORT_KEYS = ('api', 'chat', 'hub', 'workbench', 'daemon')
DEFAULT_PORTS = dict(api=4288, chat=4289, hub=4292, workbench=4294, daemon=4295)


def project_root():
    return Path(__file__).resolve().parents[1]


def default_config(root=None):
    root = Path(root or project_root()).resolve()
    suffix = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    return dict(schema_version=1, instance_id='smart-hospital-' + suffix,
                isaac_home='', assets_root='', gpu_index=0, ros_domain_id=74,
                image='smart-hospital:humble-repro-v1', ports=DEFAULT_PORTS.copy())


def read_config(root=None, path=None):
    root = Path(root or project_root()).resolve()
    path = Path(path or root / 'portable/config.json')
    value = json.loads(path.read_text(encoding='utf-8'))
    required = set(default_config(root))
    if not isinstance(value, dict) or set(value) != required or value['schema_version'] != 1:
        raise ValueError('portable_config_schema_invalid')
    if not isinstance(value['instance_id'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{2,45}', value['instance_id']):
        raise ValueError('instance_id_must_be_a_short_docker_safe_name')
    if not isinstance(value['image'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9._/:-]{1,160}', value['image']):
        raise ValueError('docker_image_name_invalid')
    for key, maximum in [('gpu_index', 15), ('ros_domain_id', 101)]:
        if type(value[key]) is not int or not 0 <= value[key] <= maximum:
            raise ValueError(key + '_out_of_range')
    if not isinstance(value['ports'], dict) or set(value['ports']) != set(PORT_KEYS):
        raise ValueError('portable_ports_schema_invalid')
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in value['ports'].values()):
        raise ValueError('portable_ports_out_of_range')
    if len(set(value['ports'].values())) != len(PORT_KEYS):
        raise ValueError('portable_ports_must_be_unique')
    if any(not isinstance(value[key], str) for key in ['isaac_home', 'assets_root']):
        raise ValueError('portable_paths_must_be_strings')
    return value


def config_digest(value):
    # Installation and asset locations are runtime environment, not source bindings.
    code = {key: value[key] for key in ['schema_version', 'instance_id', 'ros_domain_id', 'image', 'ports']}
    return hashlib.sha256(json.dumps(code, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def runtime_env(root, config, check_install=True):
    root = Path(root).resolve()
    supplied_isaac = os.environ.get('ISAAC_HOME') or config['isaac_home']
    if check_install and not supplied_isaac:
        raise ValueError('ISAAC_HOME_or_config_isaac_home_required')
    isaac = Path(supplied_isaac or '/missing-isaac-installation').expanduser().resolve()
    if check_install and not (isaac / 'python.sh').is_file():
        raise ValueError('ISAAC_HOME_must_contain_python_sh')
    env = os.environ.copy()
    env.update(ISAAC_HOME=str(isaac), HOSPITAL_ROOT=str(root),
               HOSPITAL_INSTANCE_ID=config['instance_id'], HOSPITAL_IMAGE=config['image'],
               HOSPITAL_GPU_INDEX=str(config['gpu_index']), HOSPITAL_ROS_DOMAIN_ID=str(config['ros_domain_id']),
               ROS_DOMAIN_ID=str(config['ros_domain_id']), HOSPITAL_MODE='smart-ward-v1',
               HOSPITAL_DATA_DIR=str(root / 'runs/smart-ward-v1'),
               HOSPITAL_MHS_HUB='ws://127.0.0.1:' + str(config['ports']['hub']),
               AGH_HOME=str(root / 'agent/agh-home-mhs'), AGNES_PROFILE='local-dev',
               AGNES_WEB_ORIGIN='http://127.0.0.1:' + str(config['ports']['chat']),
               AGNES_HUB_LISTEN='127.0.0.1:' + str(config['ports']['hub']),
               AGNES_HUB_ORIGIN='http://127.0.0.1:' + str(config['ports']['hub']),
               AGNES_HUB_DATA=str(root / 'agent/agh-home-mhs/hub'),
               AGNES_MCP_STDIO_ALLOWLIST=str(root / 'mhs/mcp.sh'))
    for name, port in config['ports'].items():
        env['HOSPITAL_' + name.upper() + '_PORT'] = str(port)
    assets = os.environ.get('HOSPITAL_ASSETS_ROOT') or config['assets_root']
    if assets:
        env['HOSPITAL_ASSETS_ROOT'] = assets.rstrip('/')
    # No provider credentials, server IP, SSH key, Docker socket or proxy is set here.
    return env
