"""Keep full Nav2 validation paths out of frequently copied business state."""
import copy
import hashlib
import json
import os
from pathlib import Path


EVIDENCE_KEY = 'nav2_path_evidence'


def route_summary(route):
    """The coarse candidate is still needed by the map and route selection UI."""
    return copy.deepcopy({key: value for key, value in route.items()
                          if key not in {'nav2_path', '_cells'}})


def write_route_evidence(root, data_dir, run_id, route):
    """Write exact validation coordinates once, never overwrite prior evidence.

    This is called outside the engine lock. The cached route remains the motion
    source; this artifact is a proof of its validation, not a replay instruction.
    A failed/partial existing artifact cannot be silently accepted or replaced.
    """
    document = {'schema': 'hospital-nav2-route-evidence-v1', 'run_id': run_id,
                'route': {key: value for key, value in route.items()
                          if key != EVIDENCE_KEY}}
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    digest = hashlib.sha256(payload).hexdigest()
    folder = Path(data_dir) / run_id / 'route-evidence'
    path = folder / (digest + '.json')
    relative = path.resolve().relative_to(Path(root).resolve()).as_posix()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        # Non-truncating mode also permits fsync on Windows after a prior write
        # whose fsync failed. No existing artifact bytes are ever modified.
        with path.open('r+b') as stream:
            if stream.read() != payload:
                raise ValueError('route_evidence_hash_mismatch')
            os.fsync(stream.fileno())
    return {'path': relative, 'sha256': digest, 'size_bytes': len(payload),
            'nav2_path_points': len(route.get('nav2_path', [])),
            'schema': document['schema']}
