"""Project-only runtime selection. Legacy data stays at its original path."""
import json
import os
from pathlib import Path

SMART_MODE = 'smart-ward-v1'


def mode(root):
    selected = os.environ.get('HOSPITAL_MODE')
    if selected is None:
        path = Path(root) / 'configs/runtime-profile.json'
        selected = json.loads(path.read_text(encoding='utf-8')).get('mode') if path.exists() else 'mhs-v2'
    if selected not in {'mhs-v2', SMART_MODE}:
        raise ValueError('unknown_hospital_runtime_mode')
    return selected


def data_dir(root):
    return Path(root) / 'runs' / mode(root)


def map_yaml(root):
    if mode(root) == SMART_MODE:
        return data_dir(root) / 'scene/map/carter_hospital_smart_ward.yaml'
    return Path(root) / 'maps/carter_hospital_navigation.yaml'


def map_image(root):
    # Both map variants are generated with a same-directory PNG.
    return map_yaml(root).with_suffix('.png')
