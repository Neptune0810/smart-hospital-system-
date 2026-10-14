"""Run/epoch-scoped immutable normal scene queue and ACK originals."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time


CHANNELS = frozenset({'commands', 'acks', 'withheld-acks', 'fault-history'})
MAX_BYTES = 1024*1024


class SceneIOConflict(ValueError):
    pass


def _part(value):
    if not isinstance(value, str) or value in {'.', '..'} or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', value):
        raise ValueError('invalid_scene_scope_component')
    return value


def scene_path(scene_dir, channel, run_id, epoch, operation_id):
    if channel not in CHANNELS: raise ValueError('invalid_scene_channel')
    return Path(scene_dir)/channel/_part(run_id)/_part(epoch)/(_part(operation_id)+'.json')


def _identity(command):
    if not isinstance(command, dict): raise ValueError('scene_command_not_object')
    return tuple(_part(command.get(key)) for key in ('run_id', 'epoch', 'operation_id'))


def _encoded(value):
    raw = (json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False, indent=2)+'\n').encode('utf-8')
    if len(raw) > MAX_BYTES: raise ValueError('scene_payload_too_large')
    return raw


def _read(path):
    _parent(path, create=False)
    info = path.lstat()
    # Publication briefly has the immutable original and the writer's staging
    # hardlink. Wait only for that link to be removed, never accept aliased data.
    for _ in range(10):
        if info.st_nlink != 2: break
        time.sleep(.005);info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
        raise SceneIOConflict('scene_original_not_bounded_single_link_file')
    descriptor = os.open(path, os.O_RDONLY|getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno());raw = stream.read(MAX_BYTES+1);after = os.fstat(stream.fileno())
    stamp = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns)
    if len(raw) > MAX_BYTES or stamp(info) != stamp(opened) or stamp(opened) != stamp(after) or stamp(after) != stamp(path.lstat()):
        raise SceneIOConflict('scene_original_changed_during_read')
    return raw


def _parent(path, create=True):
    # Validate existing ancestors BEFORE creating a child below them.
    for parent in reversed(path.parents[:4]):
        try: info = parent.lstat()
        except FileNotFoundError:
            if not create: raise
            try:
                parent.mkdir(parents=(parent == path.parents[3]), exist_ok=False)
                _sync_directory(parent.parent)
            except FileExistsError: pass  # Concurrent creator still must pass lstat.
            info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise SceneIOConflict('scene_scope_directory_not_real')


def _sync_directory(path):
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY|getattr(os, 'O_DIRECTORY', 0))
        try: os.fsync(descriptor)
        finally: os.close(descriptor)


def _publish(path, raw):
    _parent(path)
    if path.exists() or path.is_symlink():
        if _read(path) != raw: raise SceneIOConflict('scene_same_scope_payload_conflict')
        return
    descriptor, name = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        try: os.link(temporary, path)
        except FileExistsError:
            if _read(path) != raw: raise SceneIOConflict('scene_same_scope_payload_conflict')
        # Remove only this writer's owned staging link before reading originals.
        temporary.unlink();_sync_directory(path.parent)
    finally:
        if temporary.exists(): temporary.unlink()


def publish_command(scene_dir, command):
    scope = _identity(command)
    if not isinstance(command.get('kind'), str) or not isinstance(command.get('arguments'), dict):
        raise ValueError('invalid_scene_command_payload')
    path = scene_path(scene_dir, 'commands', *scope)
    _publish(path, _encoded(command))
    return path


def iter_commands(scene_dir, run_id, epoch):
    path = scene_path(scene_dir, 'commands', run_id, epoch, 'scope-probe')
    try: _parent(path, create=False)
    except FileNotFoundError: return []
    folder = path.parent
    return sorted(folder.glob('*.json'))


def _ack(command, raw):
    scope = _identity(command)
    try: ack = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError) as error: raise SceneIOConflict('scene_ack_invalid_json') from error
    if not isinstance(ack, dict) or tuple(ack.get(key) for key in ('run_id', 'epoch', 'operation_id')) != scope:
        raise SceneIOConflict('scene_ack_scope_mismatch')
    return ack


def read_ack(scene_dir, command, *, channel='acks'):
    return _ack(command, _read(scene_path(scene_dir, channel, *_identity(command))))


def publish_ack_bytes(scene_dir, command, raw, *, channel='acks'):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES: raise ValueError('invalid_scene_ack_bytes')
    ack = _ack(command, raw)
    _publish(scene_path(scene_dir, channel, *_identity(command)), raw)
    return ack


def write_ack(scene_dir, command, ack, *, channel='acks'):
    return publish_ack_bytes(scene_dir, command, _encoded(ack), channel=channel)


def default_operation_id(run_id, epoch, device_id, tool_name, call_id):
    _part(run_id);_part(epoch);_part(device_id);_part(tool_name)
    if not isinstance(call_id, str) or not 1 <= len(call_id) <= 1024: raise ValueError('invalid_mhs_call_identity')
    raw = json.dumps(['scene-op-v121', run_id, epoch, device_id, tool_name, call_id], ensure_ascii=False,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return 'mhs-'+hashlib.sha256(raw).hexdigest()[:40]
