"""Durable, project-scoped smart-ward tasks; scene feedback is authoritative.

Notifications are at-least-once. Incident/order and scene-transfer identities,
not a claim of exactly-once model execution, prevent repeated physical work.
"""
import copy
import hashlib
import json
import math
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .engine import ACTIVE
from .engine_v2 import HospitalV2
from .runtime_diagnostics import record
from .scene_io import SceneIOConflict, publish_command, read_ack, scene_path


CABINETS = {'hospital-cabinet-b': 'station_b', 'hospital-cabinet-c': 'station_c',
            'hospital-cabinet-lab': 'lab'}
LIGHTS = {'hospital-light-301': 'room301', 'hospital-light-303': 'room303'}
SENSORS = {'hospital-sensor-301': 'room301', 'hospital-sensor-303': 'room303'}
TERMINAL_INCIDENTS = {'resolved', 'unresolved'}
MAX_SAMPLE_AGE = 3
NAVIGATION_QUALIFICATION_VERSION = 'smart-nav-20261013.3'
NAVIGATION_QUALIFICATION_GLOBAL_SECONDS = 7200
NAVIGATION_QUALIFICATION_SEGMENT_SECONDS = 2400
EPISODE_REPORT_SCHEMA = 'smart-ward-episode-report-20261013.1'
MODEL_ADMISSION_SCHEMA = 'hospital-model-admission89-v1'


def _id(prefix, *parts):
    return prefix + hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()[:20]


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, indent=2)
    temporary = path.with_name('.' + path.name + '.tmp-' + uuid.uuid4().hex)
    owned = pending_error = None
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            created = os.fstat(stream.fileno())
            owned = (created.st_dev, created.st_ino)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException as failure:
        pending_error = failure
        raise
    finally:
        if owned is not None:
            try:
                current = temporary.lstat()
                if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == owned:
                    temporary.unlink()
            except FileNotFoundError:
                pass  # Successful replace already consumed this invocation's file.
            except OSError:
                if pending_error is None:
                    raise  # Cleanup must not hide the original write/replace error.


def _report_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _read_report_bytes(path, expected_sha, expected_size):
    if path.is_symlink() or not path.is_file():
        raise ValueError('episode_report_original_missing_or_not_regular')
    raw = path.read_bytes()
    if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('episode_report_original_digest_mismatch')
    return raw


def _sync_report_directory(folder):
    if hasattr(os, 'O_DIRECTORY'):
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _prepare_report_bytes(path, raw):
    """Never truncate an original. Partial/failed writes remain for diagnosis."""
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = hashlib.sha256(raw).hexdigest()
    if path.exists() or path.is_symlink():
        _read_report_bytes(path, expected, len(raw))
        with path.open('r+b') as stream:
            os.fsync(stream.fileno())
        _sync_report_directory(path.parent)
        return
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    _read_report_bytes(path, expected, len(raw))
    _sync_report_directory(path.parent)


def _publish_report_bytes(prepared, target, expected_sha, expected_size):
    """Exclusive same-filesystem publication; existing final bytes are immutable."""
    if not (target.exists() or target.is_symlink()):
        _read_report_bytes(prepared, expected_sha, expected_size)
        try:
            os.link(prepared, target)
        except FileExistsError:
            pass
    raw = _read_report_bytes(target, expected_sha, expected_size)
    _sync_report_directory(target.parent)
    if prepared.exists() and not prepared.is_symlink():
        original, published = prepared.stat(), target.stat()
        if (original.st_dev, original.st_ino) == (published.st_dev, published.st_ino):
            prepared.unlink()  # Only this seal's second name; final remains intact.
            _sync_report_directory(target.parent)
    return raw


def _latest_report_bytes(path, raw):
    temporary = path.with_name('.' + path.name + '.episode-tmp-' + uuid.uuid4().hex)
    owned = None
    try:
        with temporary.open('xb') as stream:
            current = os.fstat(stream.fileno())
            owned = (current.st_dev, current.st_ino)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_report_directory(path.parent)
    finally:
        if owned is not None:
            try:
                current = temporary.lstat()
                if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == owned:
                    temporary.unlink()
            except FileNotFoundError:
                pass


class SmartWardV1(HospitalV2):
    def __init__(self, root, stations, require_sim_truth=False, **kwargs):
        self._transaction_depth = 0
        self._transaction_events = []
        self._ward_ready = False
        self._seen_scene_seq = None
        self._low_samples = {}
        self._good_samples = {}
        config_path = Path(root) / 'configs/smart-ward-v1.json'
        self.ward_config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
        kwargs['data_dir'] = Path(root) / 'runs/smart-ward-v1'
        restored_snapshot = (kwargs['data_dir'] / 'state.sqlite').exists()
        super().__init__(root, stations, require_sim_truth=require_sim_truth, **kwargs)
        self.s['protocol'] = 'smart-ward-v1'
        self.s.setdefault('smart_ward', self._new_ward())
        self.db.execute('CREATE TABLE IF NOT EXISTS ward_notifications '
                        '(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS ward_model_admissions '
                        '(dispatch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, episode INTEGER NOT NULL, '
                        'scene_epoch TEXT NOT NULL, session_key TEXT NOT NULL, budget_start_wall REAL NOT NULL, '
                        'metadata_hash TEXT NOT NULL, reserved_wall REAL NOT NULL, reserved_sim REAL NOT NULL, '
                        'ordinal INTEGER NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS ward_model_admission_episode '
                        'ON ward_model_admissions(run_id,episode)')
        self.db.commit()
        self.scene_dir = self.data_dir / 'scene'
        self.scene_dir.mkdir(parents=True, exist_ok=True)
        self._ward_ready = True
        ward = self.s['smart_ward']
        if restored_snapshot and ward['watch'].get('enabled'):
            ward['watch'].update(enabled=False, phase='paused')
            ward['resume_required'] = True
            ward['pause_wall'] = time.time()
            ward['pause_sim'] = self.s['sim_time']
        qualification = ward.get('navigation_qualification')
        if restored_snapshot and qualification and qualification.get('active'):
            qualification.update(active=False, ended_wall=time.time(), reason='backend_restarted')
        fault_qualification = ward.get('fault_qualification')
        if restored_snapshot and fault_qualification and fault_qualification.get('active'):
            fault_qualification.update(active=False, ended_wall=time.time(), reason='backend_restarted',
                                       ended_scene_seq=(ward.get('scene') or {}).get('seq'))
        for task in self.s.get('navigation_tasks', {}).values():
            if restored_snapshot and task.get('status') in ACTIVE | {'unknown'}:
                task.update(status='unknown', reason='backend_restarted_motion_requires_reconciliation')
                ward['navigation_recovery_required'] = True
                ward['navigation_recovery_stop_requested'] = False
                recovery = ward.setdefault('navigation_recovery_tasks', [])
                if task['nav_task_id'] not in recovery:
                    recovery.append(task['nav_task_id'])
        self._control()
        self.save()
        self._publish_commands()

    @staticmethod
    def _new_ward():
        return {'watch': {'enabled': False, 'phase': 'stopped', 'session_id': None},
                'incidents': {}, 'devices': {}, 'specimens': {}, 'transfers': {},
                'operations': {}, 'service_plan': None, 'maintenance_tickets': [],
                'scene': None, 'episode': 0, 'model_request_ids': [], 'recovery_required': False}

    def new_state(self):
        state = super().new_state()
        state['smart_ward'] = self._new_ward()
        return state

    def _control(self):
        ward = self.s['smart_ward']
        if not ward.get('epoch'):
            ward['epoch'] = uuid.uuid4().hex
        expected = {'run_id': self.s['run_id'], 'epoch': ward['epoch'],
                    'enabled': bool(ward['watch']['enabled']),
                    'stop_requested': bool(self.s.get('operator_stopped') or ward.get('recovery_required')
                                           or ward.get('resume_required') or ward.get('navigation_recovery_required'))}
        if ward.get('fault_qualification'):
            fq = ward['fault_qualification']
            expected['fault_qualification'] = {key: fq[key] for key in
                ['qualification_id', 'session_key', 'started_wall', 'deadline_wall']}
            expected['fault_qualification']['active'] = bool(fq.get('active') and not self.s.get('operator_stopped'))
            expected['fault_qualification']['simulation_pause_requested'] = bool(fq.get('simulation_pause_requested'))
        path = self.scene_dir / 'control.json'
        try:
            old = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            old = None
        if old != expected:
            _atomic_json(path, expected)

    @contextmanager
    def _atomic(self):
        """State, incident, operation and outbox changes share one SQLite commit."""
        timing_start = time.monotonic()
        with self.lock:
            if self._transaction_depth:
                yield
                return
            timing = {'lock_wait_seconds': time.monotonic() - timing_start}
            phase_start = time.monotonic()
            before = copy.deepcopy(self.s)
            nav_before = copy.deepcopy(self.nav)
            sampling_before = (self._seen_scene_seq, copy.deepcopy(self._low_samples), copy.deepcopy(self._good_samples))
            timing['copy_seconds'] = time.monotonic() - phase_start
            self._transaction_events = []
            self.db.execute('BEGIN IMMEDIATE')
            self._transaction_depth = 1
            changed = False
            try:
                yield
                phase_start = time.monotonic()
                changed = self.s != before or bool(self._transaction_events)
                timing['compare_seconds'] = time.monotonic() - phase_start
                if changed:
                    phase_start = time.monotonic()
                    encoded = json.dumps(self.s)
                    timing['json_seconds'] = time.monotonic() - phase_start
                    timing['snapshot_bytes'] = len(encoded)
                    phase_start = time.monotonic()
                    self.db.execute('INSERT OR REPLACE INTO snapshots VALUES (1,?)', (encoded,))
                    timing['sqlite_execute_seconds'] = time.monotonic() - phase_start
                phase_start = time.monotonic()
                self.db.commit()
                timing['sqlite_commit_seconds'] = time.monotonic() - phase_start
            except BaseException:
                self.db.rollback()
                self.s = before
                self.nav = nav_before
                self._seen_scene_seq, self._low_samples, self._good_samples = sampling_before
                self._transaction_events = []
                raise
            finally:
                self._transaction_depth = 0
            # SQLite remains authoritative if an evidence-file write is interrupted.
            if changed:
                phase_start = time.monotonic()
                self._write_snapshot()
                timing['snapshot_file_seconds'] = time.monotonic() - phase_start
            for event in self._transaction_events:
                folder = self.data_dir / event.pop('_run_id')
                folder.mkdir(parents=True, exist_ok=True)
                with (folder / 'events.jsonl').open('a', encoding='utf-8') as output:
                    output.write(json.dumps(event, ensure_ascii=False) + '\n')
            self._transaction_events = []
            if self._ward_ready:
                self._control()
                self._publish_commands()
            timing['duration_seconds'] = time.monotonic() - timing_start
            self._runtime_latest_atomic = dict(timing, changed=changed)
            if timing['duration_seconds'] >= .2:
                record(self, 'ward_atomic', changed=changed, **timing)

    def _write_snapshot(self):
        folder = self.data_dir / self.s['run_id']
        _atomic_json(folder / 'state.json', self.s)

    def save(self):
        if self._transaction_depth:
            return
        started = time.monotonic()
        with self.lock:
            timing = {'lock_wait_seconds': time.monotonic() - started}
            phase = time.monotonic()
            encoded = json.dumps(self.s)
            timing.update(json_seconds=time.monotonic() - phase, snapshot_bytes=len(encoded))
            phase = time.monotonic()
            self.db.execute('INSERT OR REPLACE INTO snapshots VALUES (1,?)', (encoded,))
            timing['sqlite_execute_seconds'] = time.monotonic() - phase
            phase = time.monotonic()
            self.db.commit()
            timing['sqlite_commit_seconds'] = time.monotonic() - phase
            phase = time.monotonic()
            self._write_snapshot()
            timing['snapshot_file_seconds'] = time.monotonic() - phase
            timing['duration_seconds'] = time.monotonic() - started
            self._runtime_latest_save = dict(timing)
            if timing['duration_seconds'] >= .2:
                record(self, 'ward_save', **timing)

    def event(self, kind, **data):
        if not self._transaction_depth:
            with self._atomic():
                return self.event(kind, **data)
        if kind in {'orders_created', 'receiver_changed', 'order_deferred', 'order_failed',
                    'navigation_failed', 'handoff_rejected', 'operator_stopped', 'ward_incident_created'}:
            self.s['planning_version'] = self.s.get('planning_version', 0) + 1
        body = dict(kind=kind, wall_time=time.time(), sim_time=self.s['sim_time'], **data)
        cursor = self.db.execute('INSERT INTO events(run_id,body) VALUES (?,?)',
                                 (self.s['run_id'], json.dumps(body)))
        body['event_id'] = cursor.lastrowid
        if kind in {'ward_incident_created', 'ward_incident_resolved', 'ward_incident_unresolved',
                    'package_picked_up', 'delivery_confirmed', 'operator_stopped',
                    'ward_episode_completed', 'mhs_budget_exhausted', 'navigation_succeeded',
                    'navigation_failed', 'navigation_canceled'}:
            self.s['smart_ward']['verification_event_version'] = cursor.lastrowid
        self._transaction_events.append(dict(body, _run_id=self.s['run_id']))
        return body

    def _operation(self, name, arguments, action):
        op = arguments.get('operation_id')
        if not isinstance(op, str) or not 1 <= len(op) <= 160 or op in {'.', '..'} or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.' for c in op):
            raise ValueError('invalid_operation_id')
        key = self.s['run_id'] + ':' + op
        signature = json.dumps([name, arguments], sort_keys=True)
        with self._atomic():
            row = self.db.execute('SELECT signature,body FROM operations WHERE id=?', (key,)).fetchone()
            if row:
                if row[0] != signature:
                    raise ValueError('operation_id_conflict')
                if name in {'device_action', 'ward_command', 'fault_qualification_command'}:
                    recorded = json.loads(row[1])
                    operations = (self.s['smart_ward'].get('fault_qualification', {}).get('operations', {})
                                  if name == 'fault_qualification_command' else self.s['smart_ward']['operations'])
                    current = operations.get(recorded.get('operation_id'))
                    if current:
                        return copy.deepcopy(current)
                return json.loads(row[1])
            result = action()
            self.db.execute('INSERT INTO operations VALUES (?,?,?)', (key, signature, json.dumps(result)))
            return copy.deepcopy(result)

    def _scene_fresh(self):
        scene = self.s.get('smart_ward', {}).get('scene')
        return bool(scene and scene.get('run_id') == self.s['run_id']
                    and scene.get('epoch') == self.s['smart_ward']['epoch']
                    and 0 <= time.time() - scene.get('wall_time', 0) <= self.ward_config.get('freshness_seconds', MAX_SAMPLE_AGE))

    def _require_active(self):
        ward = self.s['smart_ward']
        if not ward['watch']['enabled']:
            raise ValueError('smart_ward_watch_not_enabled')
        if self.s.get('operator_stopped') or ward.get('recovery_required'):
            raise ValueError('smart_ward_recovery_or_new_run_required')
        if ward.get('navigation_recovery_required'):
            raise ValueError('prior_navigation_stop_requires_reconciliation')
        if (self.s.get('mhs_budget') or {}).get('stopped_reason'):
            raise ValueError('run_budget_exhausted')

    def start_watch(self, session_key, expected_run_id, operation_id):
        def start():
            if expected_run_id != self.s['run_id']:
                raise ValueError('session_run_mismatch')
            if self.s.get('operator_stopped') or self.s['smart_ward'].get('recovery_required'):
                raise ValueError('smart_ward_recovery_or_new_run_required')
            if self.s['smart_ward'].get('navigation_qualification'):
                raise ValueError('navigation_qualification_run_requires_reset')
            fq = self.s['smart_ward'].get('fault_qualification')
            if fq and (not fq.get('active') or session_key != fq['session_key'] or time.time() >= fq['deadline_wall']):
                raise ValueError('fault_qualification_not_active_for_this_session')
            if not self.stopped() or not self.fresh() or not self._scene_fresh():
                raise ValueError('smart_ward_start_requires_fresh_scene_and_stopped_robot')
            if self.s['smart_ward'].get('navigation_recovery_required'):
                raise ValueError('prior_navigation_stop_requires_reconciliation')
            if any(o['status'] in {'pending', 'unknown'} and o['command']['kind'] == 'handoff' for o in self.s['smart_ward']['operations'].values()):
                raise ValueError('pending_handoff_must_be_reconciled_before_resume')
            self.claim_session(session_key, expected_run_id)
            ward = self.s['smart_ward']
            watch = ward['watch']
            if ward.get('resume_required'):
                paused_wall = max(0, time.time() - ward.pop('pause_wall', time.time()))
                paused_sim = max(0, self.s['sim_time'] - ward.pop('pause_sim', self.s['sim_time']))
                ward.setdefault('pauses', []).append(dict(wall_seconds=paused_wall, sim_seconds=paused_sim,
                                                         resumed_wall=time.time(), resumed_sim=self.s['sim_time']))
            ward['resume_required'] = False
            watch.update(enabled=True, phase='watching', session_id=session_key, started_wall=time.time())
            self.event('ward_watch_started', session_id=session_key)
            self._detect_incidents()
            return {'ok': True, 'watch': copy.deepcopy(watch), 'run_id': self.s['run_id']}
        return self._operation('start_watch', dict(session_key=session_key,
                               expected_run_id=expected_run_id, operation_id=operation_id), start)

    def begin_navigation_qualification(self, session_key, expected_run_id, operation_id):
        """Explicit admin-only, bounded empty-run navigation qualification."""
        def begin():
            ward = self.s['smart_ward']
            if expected_run_id != self.s['run_id']:
                raise ValueError('session_run_mismatch')
            if ward['watch']['enabled'] or self.s['orders'] or ward['incidents'] or self.s.get('mhs_budget'):
                raise ValueError('qualification_requires_new_empty_run_with_watch_off')
            previous = ward.get('navigation_qualification')
            if previous:
                if previous.get('active') or previous.get('reason') != 'qualification_end' or previous.get('segment', 1) >= 3:
                    raise ValueError('qualification_not_continuable_reset_required')
                if previous['session_key'] != session_key or time.time() >= previous['total_deadline_wall'] or previous['attempts'] >= previous['max_attempts']:
                    raise ValueError('qualification_total_budget_or_session_invalid')
            elif self.s.get('navigation_tasks'):
                raise ValueError('qualification_requires_new_navigation_run')
            if self.s.get('operator_stopped') or ward.get('recovery_required') or ward.get('resume_required') or ward.get('navigation_recovery_required'):
                raise ValueError('qualification_run_not_ready')
            if not self.stopped() or not self.fresh() or not self._scene_fresh() or not self.guard.healthy() or not self.backend or not self.backend.ready():
                raise ValueError('qualification_requires_healthy_fresh_scene_and_stopped_robot')
            self.claim_session(session_key, expected_run_id)
            now = time.time()
            q = previous or dict(version=NAVIGATION_QUALIFICATION_VERSION, session_key=session_key,
                     started_wall=now, total_deadline_wall=now + NAVIGATION_QUALIFICATION_GLOBAL_SECONDS,
                     allowed_stations=['station_b', 'lab', 'station_c', 'depot'], attempts=0, max_attempts=12,
                     segment=0, segments=[])
            q.update(active=True, segment=q['segment'] + 1, segment_started_wall=now,
                     deadline_wall=min(now + NAVIGATION_QUALIFICATION_SEGMENT_SECONDS, q['total_deadline_wall']))
            q.pop('ended_wall', None)
            q.pop('reason', None)
            ward['navigation_qualification'] = q
            self.event('ward_navigation_qualification_started', qualification=copy.deepcopy(q))
            return dict(ok=True, run_id=self.s['run_id'], qualification=copy.deepcopy(q))
        with self.lock:
            previous = self.s['smart_ward'].get('navigation_qualification')
            # Check before idempotent replay too: never upgrade or extend a
            # qualification created under an absent or older budget version.
            if previous and previous.get('version') != NAVIGATION_QUALIFICATION_VERSION:
                raise ValueError('navigation_qualification_version_requires_new_run')
            return self._operation('begin_navigation_qualification', dict(session_key=session_key,
                expected_run_id=expected_run_id, operation_id=operation_id), begin)

    def end_navigation_qualification(self, expected_run_id, operation_id, reason='qualification_end'):
        def end():
            if expected_run_id != self.s['run_id']:
                raise ValueError('session_run_mismatch')
            q = self.s['smart_ward'].get('navigation_qualification')
            if not q:
                raise ValueError('navigation_qualification_not_started')
            if q.get('active'):
                q.update(active=False, ended_wall=time.time(), reason=reason)
                q['segments'].append(dict(segment=q['segment'], started_wall=q['segment_started_wall'],
                                          ended_wall=q['ended_wall'], reason=reason, attempts=q['attempts']))
                self.event('ward_navigation_qualification_ended', operation_id=operation_id, reason=reason)
            stop = self.request_stop(reason)
            return dict(ok=True, run_id=self.s['run_id'], qualification=copy.deepcopy(q), stop=stop)
        result = self._operation('end_navigation_qualification', dict(expected_run_id=expected_run_id,
                                 operation_id=operation_id, reason=reason), end)
        _atomic_json(self.data_dir / self.s['run_id'] / 'qualification-report.json', self.verify())
        return result

    def begin_fault_qualification(self, session_key, expected_run_id, expected_epoch,
                                  operation_id, duration_seconds=240):
        """Admin-only real fault tests; this grants no navigation bypass."""
        def begin():
            ward = self.s['smart_ward']
            if expected_run_id != self.s['run_id'] or expected_epoch != ward['epoch']:
                raise ValueError('fault_qualification_run_or_epoch_mismatch')
            if not isinstance(session_key, str) or not session_key.startswith('qualification:') or len(session_key) > 200:
                raise ValueError('fault_qualification_session_required')
            if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) or not 1 <= duration_seconds <= 1800:
                raise ValueError('fault_qualification_duration_must_be_1_to_1800_wall_seconds')
            if self.s.get('controller_session') or ward['watch']['enabled'] or self.s['orders'] or ward['specimens'] or ward['incidents'] or self.s.get('mhs_budget'):
                raise ValueError('fault_qualification_requires_unclaimed_empty_run')
            if ward.get('fault_qualification') or ward.get('navigation_qualification') or self.s.get('navigation_tasks'):
                raise ValueError('fault_qualification_requires_independent_new_run')
            if self.s.get('operator_stopped') or ward.get('recovery_required') or ward.get('resume_required') or ward.get('navigation_recovery_required'):
                raise ValueError('fault_qualification_run_not_ready')
            if not self.stopped() or not self.fresh() or not self._scene_fresh() or not self.guard.healthy() or not self.backend or not self.backend.ready():
                raise ValueError('fault_qualification_requires_fresh_healthy_stopped_robot')
            self.claim_session(session_key, expected_run_id)
            now = time.time()
            fq = dict(qualification_id=uuid.uuid4().hex, active=True, session_key=session_key,
                      run_id=self.s['run_id'], epoch=ward['epoch'], started_wall=now,
                      deadline_wall=now + duration_seconds, duration_seconds=duration_seconds,
                      commands_used=0, command_limit=20, operations={}, held_ack_operation=None,
                      simulation_pause_requested=False)
            ward['fault_qualification'] = fq
            self.event('ward_fault_qualification_started', qualification={k: v for k, v in fq.items() if k != 'operations'})
            return dict(ok=True, run_id=self.s['run_id'], qualification=copy.deepcopy(fq))
        return self._operation('begin_fault_qualification', dict(session_key=session_key,
            expected_run_id=expected_run_id, expected_epoch=expected_epoch, operation_id=operation_id,
            duration_seconds=duration_seconds), begin)

    def _fault_qualification(self, qualification_id, session_key, expected_run_id, expected_epoch, require_active=True):
        ward = self.s['smart_ward']
        fq = ward.get('fault_qualification')
        if (expected_run_id != self.s['run_id'] or expected_epoch != ward['epoch'] or not fq
                or fq['qualification_id'] != qualification_id or fq['run_id'] != expected_run_id
                or fq['epoch'] != expected_epoch):
            raise ValueError('fault_qualification_identity_mismatch')
        if fq['session_key'] != session_key or self.s.get('controller_session') != session_key:
            raise ValueError('fault_qualification_session_mismatch')
        if require_active and (not fq.get('active') or time.time() >= fq['deadline_wall'] or self.s.get('operator_stopped')):
            raise ValueError('fault_qualification_inactive_or_expired')
        return fq

    def fault_qualification_command(self, qualification_id, session_key, expected_run_id,
                                    expected_epoch, kind, arguments, operation_id):
        def command():
            fq = self._fault_qualification(qualification_id, session_key, expected_run_id, expected_epoch)
            if fq['commands_used'] >= fq['command_limit']:
                raise ValueError('fault_qualification_command_limit')
            if not isinstance(arguments, dict):
                raise ValueError('invalid_fault_arguments')
            args = copy.deepcopy(arguments)
            if kind == 'set_cabinet_availability':
                if set(args) != {'cabinet_id', 'available'} or args['cabinet_id'] not in CABINETS or not isinstance(args['available'], bool):
                    raise ValueError('invalid_qualification_cabinet_availability')
            elif kind in {'hold_ack', 'release_ack'}:
                allowed = {'target_operation_id', 'hold_seconds'} if kind == 'hold_ack' else {'target_operation_id'}
                if not set(args) <= allowed or 'target_operation_id' not in args:
                    raise ValueError('invalid_qualification_ack_arguments')
                target = args['target_operation_id']
                item = self.s['smart_ward']['operations'].get(target)
                if not item or item['command']['kind'] != 'handoff':
                    raise ValueError('qualification_ack_requires_real_handoff')
                if kind == 'hold_ack':
                    seconds = args.setdefault('hold_seconds', 90)
                    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 90:
                        raise ValueError('qualification_ack_hold_max_90_wall_seconds')
                    if item['status'] not in {'pending', 'unknown'} or scene_path(self.scene_dir, 'acks', self.s['run_id'], self.s['smart_ward']['epoch'], target).exists():
                        raise ValueError('qualification_target_ack_already_visible_or_complete')
                    if fq.get('held_ack_operation') not in {None, target}:
                        raise ValueError('qualification_only_one_handoff_ack_hold')
                    fq['held_ack_operation'] = target
                elif fq.get('held_ack_operation') != target:
                    raise ValueError('qualification_ack_target_not_held')
            elif kind == 'pause_simulation':
                seconds = args.get('seconds')
                if set(args) != {'seconds'} or isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 5 <= seconds <= 15:
                    raise ValueError('qualification_pause_must_be_5_to_15_wall_seconds')
                if (not self.stopped() or not self.fresh() or not self._scene_fresh()
                        or self.nav and self.nav.get('status') in ACTIVE | {'unknown'}
                        or any(task.get('status') in ACTIVE | {'unknown'} for task in self.s['navigation_tasks'].values())):
                    raise ValueError('qualification_pause_requires_fresh_stopped_robot_without_navigation')
                # Pause/resume can restore simulation time, never prior motion authority.
                fq['simulation_pause_requested'] = True
                self.request_stop('simulation_pause_requested')
            elif kind == 'resume_simulation':
                if args:
                    raise ValueError('qualification_resume_takes_no_arguments')
            else:
                raise ValueError('fault_qualification_command_not_allowed')
            body = dict(run_id=expected_run_id, epoch=expected_epoch, qualification_id=qualification_id,
                        session_key=session_key, operation_id=operation_id, kind=kind,
                        arguments=args, wall_time=time.time())
            item = dict(operation_id=operation_id, status='pending', command=body, created_wall=time.time())
            fq['operations'][operation_id] = item
            fq['commands_used'] += 1
            self.event('ward_fault_command_prepared', operation_id=operation_id, command_kind=kind)
            return copy.deepcopy(item)
        return self._operation('fault_qualification_command', dict(qualification_id=qualification_id,
            session_key=session_key, expected_run_id=expected_run_id, expected_epoch=expected_epoch,
            kind=kind, arguments=arguments, operation_id=operation_id), command)

    def fault_qualification_operation(self, operation_id):
        self.tick()
        with self.lock:
            item = self.s['smart_ward'].get('fault_qualification', {}).get('operations', {}).get(operation_id)
            if not item:
                raise ValueError('unknown_fault_qualification_operation')
            return copy.deepcopy(item)

    def end_fault_qualification(self, qualification_id, session_key, expected_run_id,
                                expected_epoch, operation_id, reason='qualification_end'):
        arguments = dict(qualification_id=qualification_id, session_key=session_key,
                         expected_run_id=expected_run_id, expected_epoch=expected_epoch,
                         operation_id=operation_id, reason=reason)
        with self.lock:
            fq = self._fault_qualification(qualification_id, session_key, expected_run_id, expected_epoch, require_active=False)
            if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 160 or operation_id in {'.', '..'} or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.' for c in operation_id):
                raise ValueError('invalid_operation_id')
            row = self.db.execute('SELECT signature,body FROM operations WHERE id=?',
                                  (self.s['run_id'] + ':' + operation_id,)).fetchone()
            if row:
                if row[0] != json.dumps(['end_fault_qualification', arguments], sort_keys=True):
                    raise ValueError('operation_id_conflict')
                return json.loads(row[1])
            if fq.get('active'):
                self.stop_run('fault-stop-' + qualification_id, expected_run_id, reason='user_stop')
            def end():
                fq.update(active=False, ended_wall=time.time(), reason=str(reason)[:160],
                          ended_scene_seq=(self.s['smart_ward'].get('scene') or {}).get('seq'))
                self.event('ward_fault_qualification_ended', qualification_id=qualification_id, reason=fq['reason'])
                return dict(ok=True, run_id=self.s['run_id'], qualification=copy.deepcopy(fq),
                            stopped_confirmed=self.stopped())
            result = self._operation('end_fault_qualification', arguments, end)
        _atomic_json(self.data_dir / self.s['run_id'] / 'fault-qualification-report.json', self.verify())
        return result

    def stop_run(self, operation_id, expected_run_id, reason='user_stop'):
        if expected_run_id != self.s['run_id']:
            raise ValueError('session_run_mismatch')
        with self._atomic():
            watch = self.s['smart_ward']['watch']
            watch.update(enabled=False, phase='stopped')
            if not self.s.get('operator_stopped'):
                self.s['operator_stopped'] = dict(operation_id=operation_id, wall_time=time.time(), sim_time=self.s['sim_time'], reason=reason)
                self.event('operator_stopped', operation_id=operation_id, reason=reason)
            unfinished = False
            for incident in self.s['smart_ward']['incidents'].values():
                if incident['status'] not in TERMINAL_INCIDENTS:
                    unfinished = True
                    self._unresolved(incident, reason)
            for order in self.s['orders'].values():
                if order['status'] not in {'delivered', 'failed'}:
                    unfinished = True
                    order.update(status='failed', reason=reason)
                    self.event('order_failed', order_id=order['order_id'], reason=reason)
            if unfinished or reason != 'user_stop':
                self.s['smart_ward']['recovery_required'] = True
                self.s['mhs_outcome'] = 'incomplete'
            budget = self.s.get('mhs_budget')
            if budget and not budget.get('end_wall'):
                budget.update(end_wall=time.time(), end_sim=self.s['sim_time'], end_path=self.s['path_length'])
            result = self.request_stop(reason)
        _atomic_json(self.data_dir / self.s['run_id'] / 'report.json', self.verify())
        return result

    def _new_episode(self):
        ward = self.s['smart_ward']
        previous = ward.get('episode_reports', {}).get(str(ward['episode']))
        # A failed seal is a reporting boundary, not a new task or an exception
        # in the scene sampler. New scene facts remain available for re-detection.
        if previous and (previous.get('status') != 'sealed'
                or ward.get('latest_episode_report') != previous['receipt']['report_ref']):
            ward['report_seal_pending'] = True
            return False
        if previous is None:
            try:
                unindexed = self._unindexed_episode_originals()
            except (OSError, ValueError) as error:
                ward.update(report_seal_pending=True, report_seal_error=str(error)[:400])
                return False  # Scene sampling continues; no box or demand is discarded.
            if unindexed:
                ward['report_seal_pending'] = True
                return False
        ward.pop('report_seal_error', None)
        ward['report_seal_pending'] = False
        budget = self.s.get('mhs_budget')
        if budget and not budget.get('end_wall'):
            self.s['mhs_finished'] = False
            return True
        if budget and budget.get('end_wall') and (not previous or previous.get('status') != 'sealed'):
            ward['report_seal_pending'] = True
            return False
        ward['episode'] += 1
        ward['model_request_ids'] = []
        self.s['mhs_budget'] = dict(start_sim=self.s['sim_time'], start_wall=time.time(),
                                   start_path=self.s['path_length'], decisions=0, stopped_reason=None)
        self.s['mhs_finished'] = False
        self.s['mhs_outcome'] = None
        self.event('ward_episode_started', episode=ward['episode'])
        return True

    def _notify(self, incident, suffix='new'):
        if self.s['smart_ward'].get('fault_qualification', {}).get('active'):
            return  # Explicit admin qualification never submits a model prompt.
        notification_id = _id('N-', self.s['run_id'], incident['incident_id'], suffix)
        body = dict(notification_id=notification_id, run_id=self.s['run_id'],
                    session_id=self.s['smart_ward']['watch']['session_id'],
                    event_ids=[incident['incident_id']], status='pending', attempts=0,
                    text='[病区设备事件] ' + incident['summary'] + '。请读取最新病区状态并安排处理。',
                    created_wall=time.time(), worker_id=None, lease_until=0)
        self.db.execute('INSERT OR IGNORE INTO ward_notifications VALUES (?,?,?)',
                        (notification_id, self.s['run_id'], json.dumps(body)))

    def _incident(self, kind, key, notify=True, **values):
        ward = self.s['smart_ward']
        incident_id = _id('I-', self.s['run_id'], kind, key)
        if incident_id in ward['incidents']:
            return ward['incidents'][incident_id]
        if not self._new_episode():
            return None
        incident = dict(incident_id=incident_id, kind=kind, status='new', attempts=0, episode=ward['episode'],
                        created_wall=time.time(), created_sim=self.s['sim_time'], **values)
        ward['incidents'][incident_id] = incident
        ward['watch']['phase'] = 'processing'
        self.event('ward_incident_created', incident=copy.deepcopy(incident))
        if notify:
            self._notify(incident)
        return incident

    def _detect_incidents(self):
        ward = self.s['smart_ward']
        if not ward['watch']['enabled'] or self.s.get('operator_stopped') or ward.get('recovery_required') or not self._scene_fresh():
            return
        for sid, specimen in ward['specimens'].items():
            cabinet = specimen.get('location')
            if cabinet in {'hospital-cabinet-b', 'hospital-cabinet-c'} and specimen.get('sealed') is True:
                deadline = specimen.get('deadline_sim')
                expired = isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= self.s['sim_time']
                incident = self._incident('specimen_ready', sid, notify=not expired, specimen_id=sid, cabinet_id=cabinet,
                               station_id=CABINETS[cabinet], priority=specimen.get('priority', 'normal'),
                               summary=self.stations[CABINETS[cabinet]]['label'] + '有一份已封装标本待取，需送到检验交接点')
                if incident and expired and not incident.get('order_id'):
                    self._unresolved(incident, '标本取送期限已过或缺失，保持原柜待人工确认，未创建运输订单')
        for sensor, room in SENSORS.items():
            reading = ward['devices'].get(sensor, {})
            value = reading.get('relative_illumination')
            valid = reading.get('available') is True and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100
            self._low_samples[room] = self._low_samples.get(room, 0) + 1 if valid and value < 35 else 0
            self._good_samples[room] = self._good_samples.get(room, 0) + 1 if valid and value >= 50 else 0
            active = next((i for i in ward['incidents'].values() if i['kind'] == 'low_light' and i['room_id'] == room and i['status'] not in TERMINAL_INCIDENTS), None)
            if self._low_samples[room] >= 3 and active is None:
                prior = sum(i['kind'] == 'low_light' and i.get('room_id') == room for i in ward['incidents'].values())
                # A failed lamp stays one unresolved incident until a new run/repair.
                failed = any(i['kind'] == 'low_light' and i.get('room_id') == room and i['status'] == 'unresolved' for i in ward['incidents'].values())
                if not failed:
                    active = self._incident('low_light', room + ':' + str(prior), room_id=room,
                                            device_id=next(k for k, v in LIGHTS.items() if v == room),
                                            station_id=room, summary=self.stations[room]['label'] + '照明持续不足')
            if active and self._good_samples[room] >= 3:
                self._resolve(active, '连续三次新鲜仿真照度读数已恢复', reading=copy.deepcopy(reading))

    def _resolve(self, incident, reason, **evidence):
        if incident['status'] in TERMINAL_INCIDENTS:
            return
        incident.update(status='resolved', reason=reason, resolved_wall=time.time(),
                        resolved_sim=self.s['sim_time'], evidence=evidence)
        self.event('ward_incident_resolved', incident_id=incident['incident_id'], reason=reason, evidence=evidence)

    def _unresolved(self, incident, reason, ticket=False):
        if incident['status'] in TERMINAL_INCIDENTS:
            return
        incident.update(status='unresolved', reason=reason, closed_wall=time.time(), closed_sim=self.s['sim_time'])
        self.event('ward_incident_unresolved', incident_id=incident['incident_id'], reason=reason)
        if ticket:
            item = dict(ticket_id=_id('M-', incident['incident_id']), incident_id=incident['incident_id'],
                        room_id=incident.get('room_id'), status='open', reason=reason, simulated=True)
            self.s['smart_ward']['maintenance_tickets'].append(item)
        self._notify(incident, 'unresolved')

    def _block_transport(self, reason):
        ward = self.s['smart_ward']
        for incident in ward['incidents'].values():
            if incident['status'] not in TERMINAL_INCIDENTS:
                self._unresolved(incident, reason)
        for order in self.s['orders'].values():
            if order['status'] not in {'delivered', 'failed'}:
                order.update(status='failed', reason=reason)
                self.event('order_failed', order_id=order['order_id'], reason=reason)
        ward['recovery_required'] = True
        self.s['mhs_outcome'] = 'incomplete'
        self.request_stop(reason)

    def _queue_scene(self, kind, arguments, operation_id, **metadata):
        operations = self.s['smart_ward']['operations']
        command = dict(run_id=self.s['run_id'], epoch=self.s['smart_ward']['epoch'],
                       operation_id=operation_id, kind=kind, arguments=copy.deepcopy(arguments), wall_time=time.time())
        if operation_id in operations:
            old = operations[operation_id]
            if old['command']['kind'] != kind or old['command']['arguments'] != arguments:
                raise ValueError('operation_id_conflict')
            return copy.deepcopy(old)
        item = dict(operation_id=operation_id, status='pending', command=command,
                    created_wall=time.time(), created_sim=self.s['sim_time'], **metadata)
        operations[operation_id] = item
        self.event('ward_scene_command_prepared', operation_id=operation_id, command_kind=kind)
        return copy.deepcopy(item)

    def _publish_commands(self):
        conflicts = []
        for operation_id, item in self.s['smart_ward']['operations'].items():
            if item['status'] in {'pending', 'unknown'} and not item.get('publication_conflict'):
                try:
                    command = item['command']
                    if (command.get('run_id'), command.get('epoch'), command.get('operation_id')) != (self.s['run_id'], self.s['smart_ward']['epoch'], operation_id):
                        raise SceneIOConflict('business_scene_command_scope_mismatch')
                    publish_command(self.scene_dir, command)
                except SceneIOConflict as error:
                    item.update(status='unknown', error='scene_command_scope_conflict',
                                publication_conflict=str(error))
                    conflicts.append(operation_id)
        if conflicts:
            # Marked items cannot recurse into publication during this durable
            # safety transition. Never overwrite a conflicting original.
            with self._atomic():
                self.s['smart_ward']['recovery_required'] = True
                self.request_stop('scene_command_scope_conflict')
                self.event('ward_scene_publication_conflict', operation_ids=conflicts)
        fq = self.s['smart_ward'].get('fault_qualification', {})
        for operation_id, item in fq.get('operations', {}).items():
            if item['status'] in {'pending', 'unknown'}:
                path = self.scene_dir / 'fault-commands' / (operation_id + '.json')
                if not path.exists():
                    _atomic_json(path, item['command'])

    def ward_command(self, kind, arguments, operation_id):
        def command():
            if self.s.get('operator_stopped'):
                raise ValueError('run_stopped_by_operator_new_round_required')
            arguments_copy = copy.deepcopy(arguments)
            if kind == 'insert_specimen':
                if arguments_copy.get('cabinet_id') not in {'hospital-cabinet-b', 'hospital-cabinet-c'} or arguments_copy.get('priority', 'normal') not in {'normal', 'urgent'}:
                    raise ValueError('invalid_specimen_injection')
            elif kind in {'set_brightness', 'set_lamp_fault'}:
                if arguments_copy.get('room_id') not in LIGHTS.values():
                    raise ValueError('unknown_ward_room')
                if kind == 'set_brightness':
                    self._brightness(arguments_copy.get('brightness_percent'))
                elif not isinstance(arguments_copy.get('stuck'), bool):
                    raise ValueError('invalid_lamp_fault')
            elif kind == 'set_receiver':
                if not isinstance(arguments_copy.get('available'), bool):
                    raise ValueError('invalid_receiver_availability')
            else:
                raise ValueError('ward_scenario_command_not_allowed')
            return self._queue_scene(kind, arguments_copy, operation_id, origin='operator')
        return self._operation('ward_command', dict(kind=kind, arguments=arguments, operation_id=operation_id), command)

    @staticmethod
    def _brightness(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError('brightness_percent_must_be_0_to_100')

    def create_specimen_delivery(self, incident_id, operation_id):
        def create():
            self._require_active()
            ward = self.s['smart_ward']
            incident = ward['incidents'].get(incident_id)
            if not incident or incident['kind'] != 'specimen_ready':
                raise ValueError('unknown_specimen_incident')
            if incident.get('order_id'):
                return {'ok': True, 'already_created': True, 'orders': [copy.deepcopy(self.s['orders'][incident['order_id']])], 'planning_version': self.s['planning_version']}
            if incident['status'] in TERMINAL_INCIDENTS or not self._scene_fresh():
                raise ValueError('incident_terminal_or_scene_stale')
            specimen = ward['specimens'].get(incident['specimen_id'], {})
            if specimen.get('location') != incident['cabinet_id'] or not specimen.get('sealed'):
                raise ValueError('specimen_not_ready_in_source_cabinet')
            deadline = specimen.get('deadline_sim')
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= self.s['sim_time']:
                raise ValueError('specimen_deadline_expired_or_missing')
            ident = _id('', self.s['run_id'], incident_id)
            order = dict(order_id='O-' + ident, package_id=incident['specimen_id'], specimen_id=incident['specimen_id'],
                         incident_id=incident_id, pickup_station=incident['station_id'], dropoff_station='lab',
                         priority=specimen.get('priority', 'normal'), deadline_sim=deadline, package_label='封装标本')
            result = super(SmartWardV1, self).mutate('create_orders', {'orders': [order]})
            self.s['segment_order_ids'].append(order['order_id'])
            incident.update(status='assigned', order_id=order['order_id'])
            return dict(result, planning_version=self.s['planning_version'])
        return self._operation('create_specimen_delivery', dict(incident_id=incident_id, operation_id=operation_id), create)

    def create_delivery_orders(self, items, operation_id):
        raise ValueError('smart_ward_use_create_specimen_delivery_for_detected_incident')

    def _prepare_transfer(self, cabinet_id, order_id, operation_id):
        self._require_active()
        ward = self.s['smart_ward']
        order = self.s['orders'].get(order_id)
        if not order or not order.get('specimen_id'):
            raise ValueError('unknown_specimen_order')
        direction = 'receive' if cabinet_id == 'hospital-cabinet-lab' else 'pickup'
        transfer_id = _id('T-', self.s['run_id'], order_id, direction)
        old = ward['transfers'].get(transfer_id)
        if old and old['status'] != 'failed':
            return copy.deepcopy(ward['operations'][old['operation_id']])
        if any(v['status'] in {'pending', 'unknown'} and v['command']['kind'] == 'handoff' for v in ward['operations'].values()):
            raise ValueError('pending_handoff_must_be_reconciled')
        expected_station = order['pickup_station'] if direction == 'pickup' else 'lab'
        if CABINETS.get(cabinet_id) != expected_station:
            raise ValueError('cabinet_order_station_mismatch')
        if order['status'] in {'delivered', 'failed'}:
            raise ValueError('order_terminal')
        if not self._scene_fresh() or not self.at(expected_station) or not self.stopped():
            raise ValueError('handoff_requires_fresh_scene_and_real_stationary_arrival')
        sid = order['specimen_id']
        expected_location = cabinet_id if direction == 'pickup' else 'robot'
        if ward['specimens'].get(sid, {}).get('location') != expected_location:
            raise ValueError('specimen_location_mismatch')
        if direction == 'pickup':
            if sum(o['loaded'] for o in self.s['orders'].values()) >= self.s['capacity']:
                raise ValueError('capacity_exceeded')
        elif not order['loaded'] or not self.s['receivers'].get('lab', False):
            raise ValueError('receiver_unavailable_or_package_not_onboard')
        if direction == 'receive':
            if order['attempts'] >= 2:
                raise ValueError('handoff_retry_limit')
            order['attempts'] += 1
        args = dict(transfer_id=transfer_id, order_id=order_id, specimen_id=sid, cabinet_id=cabinet_id,
                    direction=direction, station_id=expected_station,
                    arrival_proof=dict(pose=copy.deepcopy(self.pose), physical_pose=self.physical_pose(), wall_time=time.time()))
        ward['transfers'][transfer_id] = dict(transfer_id=transfer_id, operation_id=operation_id,
                                              direction=direction, specimen_id=sid, status='prepared')
        incident = ward['incidents'][order['incident_id']]
        incident['status'] = 'executing' if direction == 'pickup' else 'awaiting_verification'
        return self._queue_scene('handoff', args, operation_id, transfer_id=transfer_id, incident_id=order['incident_id'])

    def device_action(self, device_id, tool, arguments):
        operation_id = arguments.get('operation_id')
        def action():
            self._require_active()
            if device_id in CABINETS and tool == 'handoff_order':
                return self._prepare_transfer(device_id, arguments.get('order_id'), operation_id)
            if device_id in LIGHTS and tool == 'set_brightness':
                self._brightness(arguments.get('brightness_percent'))
                if not self._scene_fresh():
                    raise ValueError('scene_feedback_stale')
                room = LIGHTS[device_id]
                incident = next((i for i in self.s['smart_ward']['incidents'].values() if i['kind'] == 'low_light' and i['room_id'] == room and i['status'] not in TERMINAL_INCIDENTS), None)
                if not incident:
                    raise ValueError('no_active_low_light_incident')
                if incident['attempts'] >= 2:
                    raise ValueError('light_retry_limit')
                for item in self.s['smart_ward']['operations'].values():
                    if item.get('incident_id') == incident['incident_id'] and item['status'] in {'pending', 'unknown'}:
                        return copy.deepcopy(item)
                incident.update(status='awaiting_verification', attempts=incident['attempts'] + 1,
                                attempt_start_wall=time.time(), attempt_start_sim=self.s['sim_time'])
                self._good_samples[room] = 0
                return self._queue_scene('set_brightness', {'room_id': room, 'brightness_percent': arguments['brightness_percent']}, operation_id,
                                         incident_id=incident['incident_id'], origin='agent')
            raise ValueError('smart_ward_device_tool_not_allowed')
        return self._operation('device_action', dict(device_id=device_id, tool=tool, arguments=arguments, operation_id=operation_id), action)

    def ward_operation(self, operation_id):
        self.tick()
        with self.lock:
            item = self.s['smart_ward']['operations'].get(operation_id)
            if item is None:
                raise ValueError('unknown_ward_operation')
            return copy.deepcopy(item)

    def _read_scene(self):
        try:
            scene = json.loads((self.scene_dir / 'observation.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if scene.get('run_id') != self.s['run_id'] or scene.get('epoch') != self.s['smart_ward']['epoch']:
            return
        stamp = scene.get('wall_time', 0)
        max_age = self.ward_config.get('freshness_seconds', MAX_SAMPLE_AGE)
        if not isinstance(stamp, (int, float)) or not 0 <= time.time() - stamp <= max_age:
            self._low_samples.clear()
            self._good_samples.clear()
            return
        sequence = scene.get('seq')
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence == self._seen_scene_seq:
            return
        if self._seen_scene_seq is not None and sequence < self._seen_scene_seq:
            return
        if not isinstance(scene.get('devices'), dict) or not isinstance(scene.get('specimens'), dict) or not isinstance(scene.get('transfers'), dict):
            return
        ward = self.s['smart_ward']
        prior = ward.get('scene')
        if prior and stamp - prior['wall_time'] > max_age:
            self._low_samples.clear()
            self._good_samples.clear()
        self._seen_scene_seq = sequence
        ward['scene'] = scene
        ward['devices'] = copy.deepcopy(scene['devices'])
        ward['specimens'] = copy.deepcopy(scene['specimens'])
        lab = ward['devices'].get('hospital-cabinet-lab', {})
        available = bool(lab.get('available', False))
        if self.s['receivers'].get('lab') != available:
            self.receiver('lab', available)
        self._detect_incidents()

    def _process_acks(self):
        ward = self.s['smart_ward']
        for operation_id, item in ward['operations'].items():
            if item['status'] not in {'pending', 'unknown'}:
                continue
            try:
                ack = read_ack(self.scene_dir, item['command'])
            except SceneIOConflict as error:
                item.update(status='unknown', error='scene_ack_identity_invalid', scene_io_error=str(error))
                continue
            except (OSError, ValueError):
                if time.time() - item['created_wall'] > 60:
                    item['status'] = 'unknown'
                continue
            if ack.get('run_id') != self.s['run_id'] or ack.get('epoch') != ward['epoch'] or ack.get('operation_id') != operation_id or ack.get('status') not in {'completed', 'failed'}:
                item.update(status='unknown', error='scene_ack_identity_invalid')
                continue
            if ack['status'] == 'failed':
                item.update(status='failed', error=ack.get('error', 'scene_command_failed'), ack=ack)
                if item['command']['kind'] == 'handoff':
                    transfer = ward['transfers'][item['transfer_id']]
                    transfer['status'] = 'failed'
                    incident = ward['incidents'][item['incident_id']]
                    if incident['status'] not in TERMINAL_INCIDENTS:
                        incident['status'] = 'assigned' if item['command']['arguments']['direction'] == 'pickup' else 'executing'
                    self.event('ward_handoff_failed', operation_id=operation_id, error=item['error'])
                    order = self.s['orders'][item['command']['arguments']['order_id']]
                    if item['command']['arguments']['direction'] == 'receive' and order['attempts'] >= 2:
                        self._block_transport('handoff_retry_limit')
                continue
            if item['command']['kind'] == 'handoff':
                args = item['command']['arguments']
                sid, tid = args['specimen_id'], args['transfer_id']
                result = ack.get('result', {})
                expected = 'robot' if args['direction'] == 'pickup' else args['cabinet_id']
                observed = (ward.get('scene') or {}).get('transfers', {}).get(tid, {})
                if not self._scene_fresh() or ward['specimens'].get(sid, {}).get('location') != expected:
                    item.update(status='unknown', error='awaiting_fresh_scene_handoff_observation')
                    continue
                if any(result.get(k) != args[k] for k in ['transfer_id', 'order_id', 'specimen_id', 'direction']) or result.get('location') != expected or observed.get('operation_id') != operation_id or observed.get('specimen_id') != sid or observed.get('direction') != args['direction'] or observed.get('location') != expected:
                    item.update(status='unknown', error='scene_handoff_ack_or_observation_mismatch')
                    continue
                order = self.s['orders'][args['order_id']]
                incident = ward['incidents'][order['incident_id']]
                if args['direction'] == 'pickup':
                    # A physical transfer committed before stop still needs ACK
                    # reconciliation. Update ownership without reopening work.
                    order.update(loaded=True)
                    if order['status'] != 'failed':
                        order['status'] = 'onboard'
                    if incident['status'] not in TERMINAL_INCIDENTS:
                        incident['status'] = 'executing'
                    self.event('package_picked_up', order_id=order['order_id'], transfer_id=tid, source='scene_ack')
                else:
                    receipt = dict(receipt_id=_id('R-', tid), order_id=order['order_id'], package_id=sid,
                                   station='lab', sim_time=ack.get('sim_time', self.s['sim_time']), simulated_handoff=True,
                                   arrival_pose=copy.deepcopy(args['arrival_proof']['pose']),
                                   physical_pose=copy.deepcopy(args['arrival_proof']['physical_pose']),
                                   pose_wall_time=args['arrival_proof']['wall_time'], transfer_id=tid, scene_ack=copy.deepcopy(ack))
                    order.update(status='delivered', loaded=False, receipt=receipt)
                    self.event('delivery_confirmed', receipt=receipt)
                    self._resolve(incident, '检验接收柜已确认标本接收', receipt=receipt, specimen=copy.deepcopy(ward['specimens'][sid]))
                ward['transfers'][tid].update(status='committed', location=expected)
            item.update(status='completed', result=copy.deepcopy(ack.get('result', {})), ack=ack, completed_wall=time.time())
            item.pop('error', None)
            self.event('ward_scene_command_completed', operation_id=operation_id, command_kind=item['command']['kind'])

    def _process_fault_acks(self):
        fq = self.s['smart_ward'].get('fault_qualification', {})
        for operation_id, item in fq.get('operations', {}).items():
            if item['status'] not in {'pending', 'unknown'}:
                continue
            try:
                raw = (self.scene_dir / 'fault-acks' / (operation_id + '.json')).read_bytes()
                ack = json.loads(raw.decode('utf-8'))
            except (OSError, ValueError, UnicodeError):
                if time.time() - item['created_wall'] > 60:
                    item['status'] = 'unknown'
                continue
            identity = ['run_id', 'epoch', 'qualification_id', 'session_key', 'operation_id']
            if any(ack.get(key) != item['command'][key] for key in identity) or ack.get('status') not in {'completed', 'failed'}:
                item.update(status='unknown', error='fault_ack_identity_invalid')
                continue
            item.update(status=ack['status'], ack=ack, ack_sha256=hashlib.sha256(raw).hexdigest(),
                        result=copy.deepcopy(ack.get('result', {})), completed_wall=time.time())
            if ack['status'] == 'failed':
                item['error'] = ack.get('error', 'fault_command_failed')
                if item['command']['kind'] == 'hold_ack' and fq.get('held_ack_operation') == item['command']['arguments']['target_operation_id']:
                    fq['held_ack_operation'] = None
            elif item['command']['kind'] == 'release_ack':
                fq['held_ack_operation'] = None
            if ack['status'] == 'completed':
                item.pop('error', None)
            self.event('ward_fault_command_' + ack['status'], operation_id=operation_id,
                       command_kind=item['command']['kind'], ack_sha256=item['ack_sha256'])

    def _timeouts(self):
        ward = self.s['smart_ward']
        for incident in ward['incidents'].values():
            if incident['status'] in TERMINAL_INCIDENTS:
                continue
            if incident['kind'] == 'low_light' and incident.get('attempt_start_wall'):
                expired = time.time() - incident['attempt_start_wall'] >= 60 or self.s['sim_time'] - incident['attempt_start_sim'] >= 30
                if expired:
                    incident.pop('attempt_start_wall', None)
                    incident.pop('attempt_start_sim', None)
                    # An unacknowledged command is reconciled, never replaced with a new ID.
                    pending = any(i.get('incident_id') == incident['incident_id'] and i['status'] in {'pending', 'unknown'} for i in ward['operations'].values())
                    if pending:
                        self._unresolved(incident, '灯具命令结果未知，需人工核验', ticket=True)
                    elif incident['attempts'] >= 2:
                        self._unresolved(incident, '两次调灯后新鲜照度读数仍未恢复', ticket=True)
                    else:
                        incident['status'] = 'assigned'
                        self._notify(incident, 'retry-' + str(incident['attempts']))
            if incident['kind'] == 'specimen_ready':
                since = self.s.get('receiver_unavailable_since', {}).get('lab')
                if since is not None and self.s['sim_time'] - since >= 150:
                    order = self.s['orders'].get(incident.get('order_id'))
                    if order and order['status'] not in {'delivered', 'failed'}:
                        order.update(status='failed', reason='receiver_unavailable_150_sim_seconds')
                        self.event('order_failed', order_id=order['order_id'], reason=order['reason'])
                    self._unresolved(incident, '检验接收连续不可用150仿真秒，标本未交付')
                    self._block_transport('receiver_unavailable_150_sim_seconds')

    @staticmethod
    def _valid_goal_identity(goal_uuid, action_namespace):
        return (isinstance(goal_uuid, str) and len(goal_uuid) == 32
                and goal_uuid != '0' * 32
                and all(c in '0123456789abcdef' for c in goal_uuid)
                and action_namespace in {'/carter1/navigate_to_pose', '/carter1/navigate_through_poses'})

    def nav_goal_identity(self, tid, goal_uuid, action_namespace):
        """A durable actual Nav2 UUID is required before sending a smart goal."""
        with self.lock:
            if self._transaction_depth:
                return {'persisted': False}  # Never send inside an uncommitted parent transaction.
            with self._atomic():
                if not self._valid_goal_identity(goal_uuid, action_namespace):
                    raise ValueError('invalid_nav_goal_identity')
                task = self.s['navigation_tasks'].get(tid)
                if not task or task.get('status') not in ACTIVE:
                    raise ValueError('nav_goal_task_not_active')
                if task.get('nav_goal_uuid') and (task['nav_goal_uuid'] != goal_uuid or task.get('nav_action_namespace') != action_namespace):
                    raise ValueError('nav_goal_identity_conflict')
                task.update(nav_goal_uuid=goal_uuid, nav_action_namespace=action_namespace)
                if self.nav and self.nav['nav_task_id'] == tid:
                    self.nav.update(nav_goal_uuid=goal_uuid, nav_action_namespace=action_namespace)
                self.event('navigation_goal_identity', nav_task_id=tid, nav_goal_uuid=goal_uuid,
                           nav_action_namespace=action_namespace)
            return {'persisted': True}

    def nav_result(self, tid, status, reason=None):
        """Persist callback state, its event and terminal guard stop together.

        Parent implementations call event/save more than once. A single durable
        transaction prevents those calls from repeatedly blocking ROS telemetry;
        the actual action result and all existing safety checks are unchanged.
        """
        with self._atomic():
            return super().nav_result(tid, status, reason)

    def nav_goal_uncertain(self, tid, goal_uuid, action_namespace, reason):
        with self._atomic():
            task = self.s['navigation_tasks'].get(tid)
            if not task or not self._valid_goal_identity(goal_uuid, action_namespace) or task.get('nav_goal_uuid') != goal_uuid or task.get('nav_action_namespace') != action_namespace or task.get('status') not in ACTIVE | {'unknown'}:
                return {'recorded': False}
            task.update(status='unknown', reason=reason)
            if self.nav and self.nav['nav_task_id'] == tid:
                self.nav.update(status='unknown', reason=reason)
            ward = self.s['smart_ward']
            ward['navigation_recovery_required'] = True
            tasks = ward.setdefault('navigation_recovery_tasks', [])
            if tid not in tasks:
                tasks.append(tid)
            self.request_stop('navigation_goal_result_uncertain')
            ward['navigation_recovery_stop_requested'] = True
            self.event('navigation_goal_uncertain', nav_task_id=tid, reason=reason)
            return {'recorded': True}

    def nav_goal_terminal(self, tid, goal_uuid, action_namespace, status):
        """Called only for a newly received exact-UUID terminal status sample."""
        with self._atomic():
            task = self.s['navigation_tasks'].get(tid)
            if not task or not self._valid_goal_identity(goal_uuid, action_namespace) or task.get('nav_goal_uuid') != goal_uuid or task.get('nav_action_namespace') != action_namespace or isinstance(status, bool) or status not in {4, 5, 6}:
                return {'confirmed': False}
            if task.get('status') != 'unknown':
                return {'confirmed': bool(task.get('nav_recovery_confirmation'))}
            terminal = {4: 'succeeded', 5: 'canceled', 6: 'failed'}[status]
            evidence = dict(nav_goal_uuid=goal_uuid, nav_action_namespace=action_namespace,
                            status=status, wall_time=time.time(), source='fresh_exact_GoalStatusArray')
            task.update(status=terminal, reason='reconciled_actual_nav2_terminal', completed_wall=time.time(),
                        nav_recovery_confirmation=evidence)
            if self.nav and self.nav['nav_task_id'] == tid:
                self.nav.update(task)
            self.event('navigation_' + terminal, nav_task_id=tid, recovery=evidence)
            return {'confirmed': True}

    def tick(self):
        if not self._ward_ready:
            return
        fq = self.s['smart_ward'].get('fault_qualification')
        if fq and fq.get('active') and (time.time() >= fq['deadline_wall'] or self.s.get('operator_stopped')):
            self.end_fault_qualification(fq['qualification_id'], fq['session_key'], fq['run_id'], fq['epoch'],
                'fault-auto-end-' + fq['qualification_id'],
                reason='qualification_deadline' if time.time() >= fq['deadline_wall'] else 'operator_stop')
        self.guard.tick(self.fresh())
        with self._atomic():
            self._read_scene()
            self._process_fault_acks()
            self._process_acks()
            ward = self.s['smart_ward']
            q = ward.get('navigation_qualification')
            if q and q.get('active') and time.time() >= q['deadline_wall']:
                q.update(active=False, ended_wall=time.time(), reason='qualification_wall_time_limit')
                q['segments'].append(dict(segment=q['segment'], started_wall=q['segment_started_wall'],
                                          ended_wall=q['ended_wall'], reason=q['reason'], attempts=q['attempts']))
                self.request_stop(q['reason'])
                self.event('ward_navigation_qualification_ended', reason=q['reason'])
            if ward.get('navigation_recovery_required') and self.backend and self.backend.ready():
                if not ward.get('navigation_recovery_stop_requested'):
                    self.request_stop('smart_ward_backend_restarted')
                    ward['navigation_recovery_stop_requested'] = True
                else:
                    self.guard.trip('navigation_recovery_pending')
                self.backend.zero()
                recovered = True
                for tid in ward.get('navigation_recovery_tasks', []):
                    task = self.s['navigation_tasks'].get(tid, {})
                    if task.get('status') == 'unknown' and hasattr(self.backend, 'reconcile_navigation'):
                        try:
                            self.backend.reconcile_navigation(copy.deepcopy(task))
                        except Exception as error:
                            task['recovery_error'] = str(error)
                    evidence = task.get('nav_recovery_confirmation', {})
                    if task.get('status') not in {'succeeded', 'failed', 'canceled'} or not self._valid_goal_identity(task.get('nav_goal_uuid'), task.get('nav_action_namespace')) or evidence.get('nav_goal_uuid') != task.get('nav_goal_uuid') or evidence.get('nav_action_namespace') != task.get('nav_action_namespace') or evidence.get('status') not in {4, 5, 6}:
                        recovered = False
                if recovered and ward.get('navigation_recovery_tasks') and self.stopped() and self.fresh():
                    ward['navigation_recovery_required'] = False
                    ward.pop('navigation_recovery_stop_requested', None)
                    ward['navigation_recovery_resolved_wall'] = time.time()
                    self.event('navigation_recovery_confirmed', nav_task_ids=ward['navigation_recovery_tasks'])
            if self.s.get('operator_stopped'):
                return
            if not ward['watch']['enabled']:
                return
            self._timeouts()
            budget = self.s.get('mhs_budget')
            if budget and budget.get('stopped_reason'):
                ward['watch'].update(enabled=False, phase='blocked')
                ward['recovery_required'] = True
                for incident in ward['incidents'].values():
                    if incident['status'] not in TERMINAL_INCIDENTS:
                        self._unresolved(incident, budget['stopped_reason'])
            if budget and not budget.get('end_wall') and not budget.get('stopped_reason'):
                reason = ('decision_limit' if budget['decisions'] >= 60 else
                          'sim_time_limit' if self.s['sim_time'] - budget['start_sim'] >= 2400 else
                          'wall_time_limit' if time.time() - budget['start_wall'] >= 3600 else None)
                if reason:
                    budget.update(stopped_reason=reason, end_wall=time.time(), end_sim=self.s['sim_time'], end_path=self.s['path_length'])
                    ward['watch'].update(enabled=False, phase='blocked')
                    ward['recovery_required'] = True
                    for incident in ward['incidents'].values():
                        if incident['status'] not in TERMINAL_INCIDENTS:
                            self._unresolved(incident, reason)
                    self.request_stop(reason)
                    self.event('mhs_budget_exhausted', reason=reason)
            self._end_episode_if_ready()

    def _end_episode_if_ready(self):
        ward = self.s['smart_ward']
        budget = self.s.get('mhs_budget')
        active = any(i['status'] not in TERMINAL_INCIDENTS for i in ward['incidents'].values())
        pending = any(i['status'] in {'pending', 'unknown'} and i['command']['kind'] == 'handoff' for i in ward['operations'].values())
        if budget and not budget.get('end_wall') and not active and not pending and not ward.get('navigation_recovery_required') and self.stopped() and self._final_station_ready() and not self.s.get('mhs_finished'):
            self.s['mhs_finished'] = True
            self.event('ward_episode_completed', episode=ward['episode'])
        if ward.get('recovery_required'):
            ward['watch']['phase'] = 'blocked'
        elif ward['watch']['enabled']:
            ward['watch']['phase'] = 'processing' if active or pending or not self._final_station_ready() else 'watching'

    def _episode_has_transport(self):
        # Resolved specimens still belong to this transport episode until its
        # declared final station is reached. Delivered history in older episodes
        # cannot authorize movement for a new lighting-only episode.
        return bool(any(i.get('kind') == 'specimen_ready' for i in self._episode_incidents())
                    or self._episode_orders() or any(o.get('loaded') for o in self.s['orders'].values()))

    def _final_station(self):
        if not self._episode_has_transport():
            return None  # Lighting-only standby is at the actual current pose.
        plan = self.s['smart_ward'].get('service_plan')
        if plan:
            step = next((s for s in reversed(plan['steps']) if s['action'] == 'standby'), {})
            return step.get('station_id')
        return None

    def _final_station_ready(self):
        if not self._episode_has_transport() and any(o['status'] not in {'delivered', 'failed'} for o in self.s['orders'].values()):
            return False  # Inconsistent outstanding transport must not become a pure-light completion.
        return bool(self.s['smart_ward'].get('recovery_required') or not self._final_station() or self.at(self._final_station()))

    def decision(self, decision_id, expected_run_id=None, session_key=None):
        with self._atomic():
            if expected_run_id is not None and expected_run_id != self.s['run_id']:
                raise ValueError('session_run_mismatch')
            if session_key and session_key != self.s.get('controller_session'):
                raise ValueError('another_session_controls_this_run')
            if self.s['smart_ward'].get('fault_qualification', {}).get('active'):
                raise ValueError('fault_qualification_has_no_model_calls')
            if self.s.get('operator_stopped'):
                raise ValueError('run_stopped_by_operator_new_round_required')
            return {'ok': True, 'budget': copy.deepcopy(self.s.get('mhs_budget')), 'run_id': self.s['run_id']}

    def _model_admission_context(self, session_key):
        ward, budget = self.s['smart_ward'], self.s.get('mhs_budget')
        if not isinstance(session_key, str) or not session_key or session_key != self.s.get('controller_session'):
            raise ValueError('model_admission_controller_mismatch')
        if ward.get('navigation_qualification') or ward.get('fault_qualification') or session_key.startswith('qualification:'):
            raise ValueError('model_admission_qualification_forbidden')
        self._require_active()
        if ward.get('resume_required') or ward.get('report_seal_pending'):
            raise ValueError('model_admission_reconciliation_required')
        if ward['watch'].get('session_id') != session_key:
            raise ValueError('model_admission_watch_session_mismatch')
        if not budget or budget.get('end_wall'):
            raise ValueError('model_admission_no_active_budget_idle_chat_disabled')
        if not self._scene_fresh() or not self.fresh() or not self.guard.state().get('healthy'):
            raise ValueError('model_admission_feedback_not_fresh')
        start = budget.get('start_wall')
        if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start):
            raise ValueError('model_admission_budget_identity_invalid')
        if time.time() < start or self.s['sim_time'] < budget['start_sim']:
            raise ValueError('model_admission_clock_before_budget')
        return dict(schema=MODEL_ADMISSION_SCHEMA, run_id=self.s['run_id'], session_key=session_key,
                    episode=ward['episode'], scene_epoch=ward['epoch'], budget_start_wall=start, limit=60)

    def model_admission_context(self, session_key):
        with self.lock:
            return self._model_admission_context(session_key)

    def model_admission_summary(self, run_id=None, episode=None):
        with self.lock:
            run_id = self.s['run_id'] if run_id is None else run_id
            episode = self.s['smart_ward']['episode'] if episode is None else episode
            ids = [row[0] for row in self.db.execute('SELECT dispatch_id FROM ward_model_admissions '
                'WHERE run_id=? AND episode=? ORDER BY ordinal', (run_id, episode))]
            return dict(schema=MODEL_ADMISSION_SCHEMA, episode=episode, limit=60,
                        admitted=len(ids), reservation_ids_sha256=hashlib.sha256(_report_bytes(ids)).hexdigest(),
                        actual_transport_attempts=None, actual_monetary_cost=None,
                        semantics='one_use_reservations; unsent_or_unknown_not_refunded; actual_calls_require_separate_raw_evidence')

    def reserve_model_dispatch(self, dispatch_id, expected_run_id, session_key, expected_episode,
                               expected_scene_epoch, budget_start_wall, metadata_hash):
        if (not isinstance(dispatch_id, str) or len(dispatch_id) != 36
                or any(c not in '0123456789abcdef-' for c in dispatch_id)
                or not isinstance(metadata_hash, str) or len(metadata_hash) != 64
                or any(c not in '0123456789abcdef' for c in metadata_hash)
                or type(expected_episode) is not int or expected_episode < 1
                or isinstance(budget_start_wall, bool) or not isinstance(budget_start_wall, (int, float))
                or not math.isfinite(budget_start_wall)):
            raise ValueError('model_admission_invalid_request')
        try:
            if str(uuid.UUID(dispatch_id)) != dispatch_id:
                raise ValueError('noncanonical')
        except ValueError:
            raise ValueError('model_admission_invalid_dispatch_id') from None
        with self._atomic():
            context = self._model_admission_context(session_key)
            if (expected_run_id != context['run_id'] or expected_episode != context['episode']
                    or expected_scene_epoch != context['scene_epoch'] or budget_start_wall != context['budget_start_wall']):
                raise ValueError('model_admission_scope_or_budget_changed')
            existing = self.db.execute('SELECT run_id,episode,scene_epoch,session_key,budget_start_wall,metadata_hash,ordinal '
                                       'FROM ward_model_admissions WHERE dispatch_id=?', (dispatch_id,)).fetchone()
            identity = (expected_run_id, expected_episode, expected_scene_epoch, session_key, budget_start_wall, metadata_hash)
            if existing:
                if tuple(existing[:6]) != identity:
                    raise ValueError('model_admission_operation_id_conflict')
                return dict(context, allowed=False, dispatch_id=dispatch_id,
                            reason='dispatch_id_already_reserved_no_second_dispatch', ordinal=existing[6])
            budget, now = self.s['mhs_budget'], time.time()
            admitted = self.db.execute('SELECT COUNT(*) FROM ward_model_admissions WHERE run_id=? AND episode=?',
                                      (expected_run_id, expected_episode)).fetchone()[0]
            reason = ('decision_limit' if budget.get('decisions', 0) >= 60 else
                      'model_dispatch_limit' if admitted >= 60 else
                      'wall_time_limit' if now - budget['start_wall'] >= 3600 else
                      'sim_time_limit' if self.s['sim_time'] - budget['start_sim'] >= 2400 else None)
            if reason:
                budget.update(stopped_reason=reason, end_wall=now, end_sim=self.s['sim_time'], end_path=self.s['path_length'])
                self.s['smart_ward']['watch'].update(enabled=False, phase='blocked')
                self.s['smart_ward']['recovery_required'] = True
                self.request_stop(reason)
                self.event('mhs_budget_exhausted', reason=reason)
                return dict(context, allowed=False, dispatch_id=dispatch_id, reason=reason, admitted=admitted)
            self.db.execute('INSERT INTO ward_model_admissions VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (dispatch_id, *identity, now, self.s['sim_time'], admitted + 1))
            self.event('ward_model_dispatch_reserved', dispatch_id=dispatch_id, episode=expected_episode,
                       ordinal=admitted + 1, metadata_hash=metadata_hash)
            return dict(context, allowed=True, dispatch_id=dispatch_id, ordinal=admitted + 1,
                        metadata_hash=metadata_hash, reservation_semantics='one_use_no_refund_not_actual_dispatch')

    def record_model_request(self, request_id, expected_run_id, session_key):
        with self._atomic():
            self.decision(request_id, expected_run_id, session_key)
            ward = self.s['smart_ward']
            budget = self.s.get('mhs_budget')
            if budget and not budget.get('end_wall') and request_id not in ward['model_request_ids']:
                ward['model_request_ids'].append(request_id)
                budget['decisions'] += 1
                self.event('ward_model_request', request_id=request_id, decisions=budget['decisions'])
            return {'ok': True, 'budget': copy.deepcopy(budget)}

    def publish_service_plan(self, planning_version, steps, reason, operation_id):
        def publish():
            self._require_active()
            if isinstance(planning_version, bool) or planning_version != self.s['planning_version']:
                raise ValueError('plan_state_changed_read_latest_state')
            if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 180 or not isinstance(steps, list) or not 1 <= len(steps) <= 30:
                raise ValueError('invalid_service_plan')
            incidents = self.s['smart_ward']['incidents']
            allowed = {'create_delivery', 'pickup', 'navigate', 'deliver', 'set_brightness', 'verify', 'wait', 'standby'}
            normalized = []
            lighting_only = not self._episode_has_transport()
            for step in steps:
                if not isinstance(step, dict) or step.get('action') not in allowed:
                    raise ValueError('invalid_service_plan_action')
                if lighting_only and step['action'] in {'create_delivery', 'pickup', 'navigate', 'deliver'}:
                    raise ValueError('lighting_only_plan_cannot_include_robot_transport')
                if lighting_only and step['action'] == 'standby' and step.get('station_id'):
                    raise ValueError('lighting_only_standby_must_stay_in_place_without_station_id')
                if step.get('action') != 'standby' and step.get('incident_id') not in incidents:
                    raise ValueError('unknown_service_incident')
                if step.get('device_id') and step['device_id'] not in {*CABINETS, *LIGHTS, *SENSORS, 'hospital-robot-01'}:
                    raise ValueError('unknown_service_plan_device')
                normalized.append(copy.deepcopy(step))
            active = {i['incident_id'] for i in incidents.values() if i['status'] not in TERMINAL_INCIDENTS}
            if active - {s.get('incident_id') for s in normalized}:
                raise ValueError('service_plan_missing_active_incidents')
            # Reuse V2's pickup-before-delivery, actual-capacity and coverage gates.
            # A preview may mention creating an order; after creation the version
            # changes and a complete transport plan must be published before motion.
            transport = []
            for step in normalized:
                incident = incidents.get(step.get('incident_id'), {})
                order = self.s['orders'].get(incident.get('order_id'))
                action = step['action']
                if not order or action not in {'pickup', 'navigate', 'deliver', 'wait'}:
                    continue
                station = step.get('station_id')
                if not station:
                    if action == 'navigate':
                        raise ValueError('navigate_step_station_required')
                    station = order['pickup_station'] if action == 'pickup' else order['dropoff_station']
                if station not in self.stations:
                    raise ValueError('unknown_service_plan_station')
                transport.append(dict(action=action, station_id=station, order_ids=[order['order_id']]))
            unfinished_orders = [o for o in self.s['orders'].values() if o['status'] not in {'delivered', 'failed'}]
            missing_order = any(i['kind'] == 'specimen_ready' and i['status'] not in TERMINAL_INCIDENTS and not i.get('order_id') for i in incidents.values())
            if transport and not missing_order:
                ending = next((s for s in reversed(normalized) if s['action'] == 'standby'), {})
                end_station = ending.get('station_id') or transport[-1]['station_id']
                transport.append(dict(action='standby', station_id=end_station, order_ids=[]))
                super(SmartWardV1, self).mutate('publish_delivery_plan', dict(
                    planning_version=planning_version, steps=transport, reason=reason,
                    operation_id=operation_id))
            elif unfinished_orders and not missing_order:
                raise ValueError('service_plan_missing_transport_steps')
            plan = dict(planning_version=planning_version, steps=normalized, reason=reason.strip(), wall_time=time.time())
            self.s['smart_ward']['service_plan'] = plan
            self.event('ward_service_plan_published', plan=plan)
            return {'ok': True, 'plan': copy.deepcopy(plan)}
        return self._operation('publish_service_plan', dict(planning_version=planning_version, steps=steps, reason=reason, operation_id=operation_id), publish)

    def claim_notification(self, worker_id):
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 160:
            raise ValueError('worker_identity_required')
        self.tick()
        with self._atomic():
            ward = self.s['smart_ward']
            if not ward['watch']['enabled'] or self.s.get('operator_stopped'):
                return None
            if ward.get('fault_qualification', {}).get('active'):
                return None
            now = time.time()
            if ward.get('notification_worker_id') not in {None, worker_id} and ward.get('worker_lease_until', 0) > now:
                return None
            ward.update(notification_worker_id=worker_id, worker_lease_until=now + 90)
            for notification_id, raw in self.db.execute('SELECT id,body FROM ward_notifications WHERE run_id=? ORDER BY rowid', (self.s['run_id'],)).fetchall():
                body = json.loads(raw)
                if body['session_id'] != ward['watch']['session_id']:
                    continue
                if body['status'] == 'delivered' or body['status'] == 'leased' and body.get('lease_until', 0) > now:
                    continue
                body.update(status='leased', worker_id=worker_id, lease_until=now + 90, attempts=body['attempts'] + 1)
                self.db.execute('UPDATE ward_notifications SET body=? WHERE id=?', (json.dumps(body), notification_id))
                return copy.deepcopy(body)
            return None

    def ack_notification(self, notification_id, status, details=None):
        if status not in {'delivered', 'retry'}:
            raise ValueError('invalid_notification_status')
        details = details or {}
        with self._atomic():
            row = self.db.execute('SELECT run_id,body FROM ward_notifications WHERE id=?', (notification_id,)).fetchone()
            if not row or row[0] != self.s['run_id'] or details.get('expected_run_id') != self.s['run_id']:
                raise ValueError('notification_run_mismatch')
            body = json.loads(row[1])
            if not details.get('worker_id') or details['worker_id'] != body.get('worker_id') or body['session_id'] != self.s['smart_ward']['watch']['session_id']:
                raise ValueError('notification_worker_or_session_mismatch')
            body.update(status=status, details=copy.deepcopy(details), ack_wall=time.time(), lease_until=0)
            self.db.execute('UPDATE ward_notifications SET body=? WHERE id=?', (json.dumps(body), notification_id))
            self.event('ward_notification_' + status, notification_id=notification_id, attempts=body['attempts'])
            return {'ok': True, 'notification_id': notification_id, 'status': status}

    def mutate(self, name, arguments):
        if name == 'navigate_route' and self._ward_ready:
            # Motion must validate the latest scene sample inside this operation.
            self._read_scene()
            ward = self.s['smart_ward']
            if ward.get('fault_qualification', {}).get('simulation_pause_requested'):
                raise ValueError('qualification_simulation_pause_requires_new_run_before_motion')
            if ward.get('navigation_recovery_required'):
                raise ValueError('prior_navigation_stop_requires_reconciliation')
            q = ward.get('navigation_qualification')
            if q:
                if q.get('version') != NAVIGATION_QUALIFICATION_VERSION:
                    raise ValueError('navigation_qualification_version_requires_new_run')
                if not q.get('active') or time.time() >= q['deadline_wall'] or q['attempts'] >= q['max_attempts']:
                    raise ValueError('navigation_qualification_ended_or_budget_exhausted')
                if ward['watch']['enabled'] or self.s['orders'] or ward['incidents'] or ward['specimens'] or self.s.get('controller_session') != q['session_key']:
                    raise ValueError('navigation_qualification_scope_changed')
                if not self._scene_fresh() or not self.fresh() or not self.guard.healthy():
                    raise ValueError('navigation_qualification_feedback_or_lease_stale')
                route = self.routes.selected(arguments['route_id'], self.s['closed_zones'])
                if route['destination'] not in q['allowed_stations']:
                    raise ValueError('navigation_qualification_destination_not_allowed')
                result = super().mutate(name, arguments)
                q['attempts'] += 1
                self.event('ward_navigation_qualification_motion', attempt=q['attempts'], navigation=result)
                return result
            self._require_active()
            if not self._episode_has_transport():
                raise ValueError('no_active_specimen_transport:lighting_only_requires_stationary_sensor_verification_no_robot_navigation')
            final_leg = bool(self._final_station() and not self._final_station_ready())
            if self.s.get('mhs_finished') or not final_leg and not any(i['kind'] == 'specimen_ready' and i['status'] not in TERMINAL_INCIDENTS for i in self.s['smart_ward']['incidents'].values()):
                raise ValueError('no_active_specimen_transport')
            if final_leg and not any(o['status'] not in {'delivered', 'failed'} for o in self.s['orders'].values()):
                route = self.routes.selected(arguments['route_id'], self.s['closed_zones'])
                if route['destination'] != self._final_station():
                    raise ValueError('only_published_final_station_is_allowed')
            if any(o['status'] in {'pending', 'unknown'} and o['command']['kind'] == 'handoff' for o in self.s['smart_ward']['operations'].values()):
                raise ValueError('pending_handoff_must_be_reconciled_before_motion')
        if name in {'pickup_package', 'confirm_delivery'} and self._ward_ready:
            raise ValueError('smart_ward_handoff_requires_MHS_cabinet_ack')
        result = super().mutate(name, arguments)
        if name == 'close_order' and self._ward_ready:
            self._block_transport(arguments.get('reason', 'order_closed_undelivered'))
        return result

    def do(self, name, args):
        if name == 'verify_services':
            return self.verify_services()
        if name == 'get_state':
            return self.state(args.get('since_event_id', 0))
        if name == 'verify_run':
            return self.verify(args.get('run_id'))
        return self._operation(name, args, lambda: self.mutate(name, args))

    def finished(self):
        if not self._ward_ready:
            return False
        ward = self.s['smart_ward']
        return bool(ward['incidents']) and not ward.get('navigation_recovery_required') and all(i['status'] in TERMINAL_INCIDENTS for i in ward['incidents'].values()) and self.stopped() and self._final_station_ready() and not any(o['loaded'] for o in self.s['orders'].values())

    def state(self, since_event_id=0):
        state = super().state(since_event_id)
        ward = copy.deepcopy(state.get('smart_ward', self._new_ward()))
        ward['incidents'] = list(ward['incidents'].values())
        fresh = self._scene_fresh() if self._ward_ready else False
        ward['scene_fresh'] = fresh
        for device in ward['devices'].values():
            device['fresh'] = fresh
            if not fresh:
                device['available'] = False
        robot_fresh = bool(state.get('pose_fresh') and state.get('physical_pose') and state.get('navigation_ready') and state.get('motion_guard', {}).get('healthy'))
        ward['devices']['hospital-robot-01'] = dict(kind='robot', available=robot_fresh,
            fresh=robot_fresh, capacity=state.get('capacity', 2), loaded=state.get('cargo_count', 0),
            stopped_confirmed=state.get('stopped_confirmed', False), navigation=state.get('navigation'))
        plan = ward.get('service_plan')
        if plan:
            plan['stale'] = plan['planning_version'] != self.s['planning_version']
            incidents = {i['incident_id']: i for i in ward['incidents']}
            for step in plan['steps']:
                incident = incidents.get(step.get('incident_id'), {})
                order = state['orders'].get(incident.get('order_id'), {})
                action = step['action']
                completed = (bool(order) if action == 'create_delivery' else
                    bool(order.get('loaded') or order.get('receipt')) if action == 'pickup' else
                    bool(order.get('receipt')) if action == 'deliver' else
                    incident.get('status') == 'resolved' if action in {'set_brightness', 'verify'} else
                    self.finished() if action == 'standby' else False)
                running = incident.get('status') in {'executing', 'awaiting_verification'}
                if action == 'navigate':
                    station = step.get('station_id')
                    completed = bool(station and self.at(station)) or any(
                        n.get('goal_station') == station and n.get('status') == 'succeeded'
                        and n.get('completed_wall', 0) >= plan['wall_time'] for n in state.get('navigation_tasks', {}).values())
                    nav = state.get('navigation') or {}
                    running = nav.get('goal_station') == station and nav.get('status') in ACTIVE
                step['status'] = ('completed' if completed else 'failed' if incident.get('status') == 'unresolved'
                                  else 'running' if running else 'planned')
        ward['summary'] = {status: sum(i['status'] == status for i in ward['incidents']) for status in ['new', 'assigned', 'executing', 'awaiting_verification', 'resolved', 'unresolved']}
        scene = ward.get('scene')
        ward['scene_sample'] = ({key: copy.deepcopy(scene.get(key))
            for key in ('run_id', 'epoch', 'seq', 'wall_time', 'sim_time')} if isinstance(scene, dict) else None)
        # Commands and full scene snapshots remain accessible through project APIs/evidence.
        ward.pop('scene', None)
        state['smart_ward'] = ward
        ward['model_admission'] = self.model_admission_summary(state['run_id'], ward['episode'])
        return state

    def verify_services(self, finalize=True):
        with self._atomic():
            all_incidents = list(self.s['smart_ward']['incidents'].values())
            incidents = self._episode_incidents()
            unresolved = [copy.deepcopy(i) for i in incidents if i['status'] == 'unresolved']
            pending = [copy.deepcopy(i) for i in incidents if i['status'] not in TERMINAL_INCIDENTS]
            budget = self.s.get('mhs_budget')
            transfer_pending = any(o['status'] in {'pending', 'unknown'} and o['command']['kind'] == 'handoff' for o in self.s['smart_ward']['operations'].values())
            # Admission/finalization gates still cover all real outstanding work,
            # irrespective of the reporting view's current-episode denominator.
            global_pending = any(i['status'] not in TERMINAL_INCIDENTS for i in all_incidents)
            ready_to_finalize = bool(all_incidents and not global_pending and not transfer_pending
                                     and not self.s['smart_ward'].get('navigation_recovery_required')
                                     and self.stopped() and self._final_station_ready() and budget)
            ward = self.s['smart_ward']
            if finalize and ready_to_finalize:
                # The final response is another real inference. Freeze only in
                # turn_stopping, after the Harness ledger has counted it.
                ward['last_verification'] = dict(episode=ward['episode'],
                    event_version=ward.get('verification_event_version', 0), wall_time=time.time())
            orders = self._episode_orders()
            return dict(run_id=self.s['run_id'], total=len(incidents), resolved=sum(i['status'] == 'resolved' for i in incidents),
                        unresolved=unresolved, pending=pending, incidents=copy.deepcopy(incidents),
                         specimen_delivered=sum(o['status'] == 'delivered' for o in orders),
                         receipts=[copy.deepcopy(o['receipt']) for o in orders if o.get('receipt')],
                         order_ids=[o['order_id'] for o in orders], metrics_scope='current_smart_ward_episode',
                         run_totals=self._report_run_totals(), **self._episode_measurements(),
                        maintenance_tickets=[copy.deepcopy(ticket) for ticket in ward['maintenance_tickets']
                            if ticket.get('incident_id') in {i['incident_id'] for i in incidents}],
                        all_resolved=bool(incidents) and not unresolved and not pending,
                        stopped_confirmed=self.stopped(), simulated_sensors=True, simulated_handoff=True,
                        notification_semantics='at_least_once', model_calls=(self.s.get('mhs_budget') or {}).get('decisions', 0),
                        episode=self.s['smart_ward']['episode'], end_wall=(self.s.get('mhs_budget') or {}).get('end_wall'),
                        ready_to_finalize=ready_to_finalize,
                        verification_event_version=self.s['smart_ward'].get('verification_event_version', 0),
                        verification_key=json.dumps([self.s['run_id'], self.s['smart_ward']['episode'],
                            (self.s.get('mhs_budget') or {}).get('end_wall'),
                            [[i['incident_id'], i['status']] for i in incidents]], separators=(',', ':')))

    def _episode_notification_barrier(self):
        """Do not freeze the budget before this episode's durable inputs drain.

        Caller holds self.lock, shared with outbox claim/ACK. Core is a bounded
        read-only snapshot of exactly this controller's main inbox. File opens
        reject current links and pin identity; Python SQLite still reopens a
        pathname, so this is not hostile concurrent ancestor-swap protection.
        """
        ward = self.s['smart_ward']
        current = {key for key, item in ward['incidents'].items()
                   if item.get('episode') == ward['episode']}
        delivered = []
        if not current:
            return
        if len(current) > 600:
            raise ValueError('episode_notifications_state_unknown')
        deadline = time.monotonic() + .5
        self.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            placeholders = ','.join('?' for _ in current)
            rows = self.db.execute('SELECT id,body FROM ward_notifications WHERE run_id=? '
                "AND json_extract(body,'$.session_id')=? AND EXISTS (SELECT 1 FROM json_each(body,'$.event_ids') "
                'WHERE value IN (' + placeholders + ')) LIMIT 601',
                (self.s['run_id'], self.s.get('controller_session'), *sorted(current))).fetchall()
            if time.monotonic() >= deadline:
                raise ValueError('notification_lookup_late')
        except (sqlite3.Error, ValueError) as error:
            raise ValueError('episode_notifications_state_unknown') from error
        finally:
            self.db.set_progress_handler(None, 0)
        if len(rows) > 600:
            raise ValueError('episode_notifications_state_unknown')
        for notification_id, raw in rows:
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError('invalid_notification')
                if body.get('session_id') != self.s.get('controller_session'):
                    continue
                identities = body.get('event_ids')
                if (body.get('run_id') != self.s['run_id'] or body.get('notification_id') != notification_id
                        or not isinstance(identities, list) or not identities
                        or any(not isinstance(key, str) or key not in ward['incidents'] for key in identities)
                        or len(identities) != len(set(identities))):
                    raise ValueError('invalid_notification_binding')
                if not current.intersection(identities):
                    continue  # Historical episode notifications cannot borrow this budget.
                if not set(identities) <= current:
                    raise ValueError('mixed_episode_notification')
            except (ValueError, TypeError) as error:
                raise ValueError('episode_notifications_state_unknown') from error
            if body.get('status') in {'pending', 'leased'}:
                raise ValueError('episode_notifications_pending')
            if body.get('status') != 'delivered':
                raise ValueError('episode_notifications_state_unknown')
            details = body.get('details')
            if (not isinstance(details, dict) or details.get('stage') != 'sdk_accepted'
                    or not body.get('worker_id') or details.get('worker_id') != body['worker_id']
                    or details.get('expected_run_id') != self.s['run_id']):
                raise ValueError('episode_notifications_state_unknown')
            delivered.append(body)
        if not delivered:
            return  # No current-episode notifications: no Core database dependency.
        try:
            root = self.root
            if not root.is_absolute() or root.resolve(strict=True) != root:
                raise ValueError('root_redirected')
            database = root / 'agent/agh-home-mhs/data/sessions.db'
            cursor = root
            for part in database.relative_to(root).parts:
                cursor /= part
                info = cursor.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('core_database_link')
                if cursor != database and not stat.S_ISDIR(info.st_mode):
                    raise ValueError('core_database_parent')
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('core_database_not_single_regular')
            identity = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink)
            for suffix in ('-wal', '-shm'):
                sidecar = Path(str(database) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    entry = sidecar.lstat()
                    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                        raise ValueError('core_database_sidecar_link')
            descriptor = os.open(database, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            connection = None
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_nlink) != identity:
                    raise ValueError('core_database_identity_changed')
                deadline = time.monotonic() + .5
                connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.25)
                connection.execute('PRAGMA query_only=ON')
                connection.execute('PRAGMA busy_timeout=250')
                connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                connection.execute('BEGIN')
                latest = connection.execute("SELECT seq,origin,trust,data FROM events WHERE session_key=? "
                    "AND lane=x'6d61696e' AND type='inbox' ORDER BY seq DESC LIMIT 1",
                    (self.s['controller_session'],)).fetchone()
                current_info = database.lstat()
                if ((current_info.st_dev, current_info.st_ino, current_info.st_mode, current_info.st_nlink) != identity
                        or time.monotonic() >= deadline):
                    raise ValueError('core_database_snapshot_changed_or_late')
            finally:
                if connection is not None:
                    connection.rollback()
                    connection.close()
                os.close(descriptor)
            if (not latest or type(latest[0]) is not int or latest[0] < 1
                    or latest[1:3] != ('system', 'trusted') or not isinstance(latest[3], str)
                    or len(latest[3]) > 2*1024*1024):
                raise ValueError('core_latest_inbox_unknown')
            inbox = json.loads(latest[3])
            items = inbox.get('items') if isinstance(inbox, dict) else None
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError('core_latest_inbox_invalid')
            identifiers = {body['notification_id'] for body in delivered}
            if any(item.get('commandId') in identifiers for item in items):
                raise ValueError('episode_notifications_pending')
            for body in delivered:
                queue_seq = (body.get('details') or {}).get('queue_seq')
                if queue_seq is not None and (type(queue_seq) is not int or queue_seq < 1 or latest[0] < queue_seq):
                    raise ValueError('core_inbox_precedes_notification_acceptance')
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            if isinstance(error, ValueError) and str(error) == 'episode_notifications_pending':
                raise
            raise ValueError('episode_notifications_state_unknown') from error

    def finalize_episode(self, expected_run_id, session_key):
        """Close a verified episode only after the final real inference is metered."""
        with self.lock:
            if expected_run_id != self.s['run_id']:
                raise ValueError('session_run_mismatch')
            if not session_key or session_key != self.s.get('controller_session'):
                raise ValueError('another_session_controls_this_run')
            ward = self.s['smart_ward']
            key = str(ward['episode'])
            existing = ward.get('episode_reports', {}).get(key)
            if existing and existing.get('status') == 'sealed':
                _, target = self._episode_report_paths(existing)
                _read_report_bytes(target, existing['sha256'], existing['bytes'])
                if ward.get('latest_episode_report') != existing['receipt']['report_ref']:
                    self._publish_latest_episode_report(existing)
                return copy.deepcopy(existing['receipt'])
            verified = ward.get('last_verification', {})
            if verified.get('episode') != ward['episode'] or verified.get('event_version') != ward.get('verification_event_version', 0):
                raise ValueError('current_episode_verification_required')
            report = self.verify_services(finalize=False)
            if not report['ready_to_finalize']:
                raise ValueError('episode_not_ready_to_finalize')
            self._episode_notification_barrier()
            raw = None
            try:
                if existing is None:
                    self._check_latest_episode_report_owner()
                    if self._unindexed_episode_originals():
                        raise ValueError('unindexed_episode_report_requires_reconciliation')
                    with self._atomic():
                        budget = self.s['mhs_budget']
                        if not budget.get('end_wall'):
                            budget.update(end_wall=time.time(), end_sim=self.s['sim_time'], end_path=self.s['path_length'])
                        closed = self.verify()
                        closed['model_request_ids'] = copy.deepcopy(ward['model_request_ids'])
                        raw = _report_bytes(closed)
                        checksum = hashlib.sha256(raw).hexdigest()
                        public = dict(schema=EPISODE_REPORT_SCHEMA, episode=ward['episode'],
                                      path=self.s['run_id'] + '/episode-' + key + '-report.json',
                                      sha256=checksum, bytes=len(raw))
                        receipt = dict(ok=True, run_id=self.s['run_id'], episode=ward['episode'],
                            verification_event_version=ward.get('verification_event_version', 0),
                            end_wall=budget['end_wall'], model_calls=budget['decisions'], report_ref=public)
                        existing = dict(public, status='preparing', run_id=self.s['run_id'],
                            controller_session=session_key, budget=copy.deepcopy(budget),
                            model_request_count=len(ward['model_request_ids']),
                            model_request_ids_sha256=hashlib.sha256(_report_bytes(ward['model_request_ids'])).hexdigest(),
                            prepared_name='.episode-' + key + '-report-' + checksum + '.prepared', receipt=receipt)
                        ward.setdefault('episode_reports', {})[key] = existing
                        ward['report_seal_pending'] = True
                        prepared, _ = self._episode_report_paths(existing)
                        # The original bytes and directory entry are durable
                        # before SQLite closes this batch and publishes its ref.
                        _prepare_report_bytes(prepared, raw)
                self._finish_episode_report_seal(existing)
                self._publish_latest_episode_report(existing)
                return copy.deepcopy(existing['receipt'])
            except Exception as failure:
                # Preserve both the candidate byte file and its small DB index.
                # Never rerender an unknown/partial original from newer feedback.
                try:
                    with self._atomic():
                        current_ward = self.s['smart_ward']
                        current = current_ward.get('episode_reports', {}).get(key)
                        if current is None and existing is not None:
                            # A failed preparation may leave an original prefix
                            # or complete file. Retain its exact identity, never
                            # render a replacement from later feedback.
                            current = copy.deepcopy(existing)
                            current_ward.setdefault('episode_reports', {})[key] = current
                            self.s['mhs_budget'] = copy.deepcopy(current['budget'])
                        if current and current.get('status') != 'sealed':
                            current.update(status='unknown', error=str(failure)[:400])
                            current_ward['report_seal_pending'] = True
                except Exception:
                    pass  # The original storage failure remains the returned failure.
                raise

    def _episode_incidents(self):
        ward = self.s['smart_ward']
        return [i for i in ward['incidents'].values() if i.get('episode') == ward['episode']]

    def _episode_orders(self):
        identities = sorted({i['order_id'] for i in self._episode_incidents() if i.get('order_id')})
        return [self.s['orders'][identity] for identity in identities]

    def _episode_measurements(self):
        budget = self.s.get('mhs_budget')
        if not budget:
            return dict(wall_seconds=0, elapsed_sim=0, path_length=0, model_request_count=0,
                        model_request_ids_sha256=hashlib.sha256(_report_bytes([])).hexdigest(),
                         token_usage=None, actual_monetary_cost=None, provider_admission=self.model_admission_summary(),
                        usage_source='business_core_request_ids_only; Harness ledger usage is separate evidence')
        identities = self.s['smart_ward']['model_request_ids']
        return dict(wall_seconds=budget.get('end_wall', time.time()) - budget['start_wall'],
                    elapsed_sim=budget.get('end_sim', self.s['sim_time']) - budget['start_sim'],
                    path_length=budget.get('end_path', self.s['path_length']) - budget.get('start_path', 0),
                    model_request_count=len(identities),
                    model_request_ids_sha256=hashlib.sha256(_report_bytes(identities)).hexdigest(),
                     token_usage=None, actual_monetary_cost=None, provider_admission=self.model_admission_summary(),
                    usage_source='business_core_request_ids_only; Harness ledger usage is separate evidence')

    def _report_run_totals(self):
        incidents = list(self.s['smart_ward']['incidents'].values())
        orders = list(self.s['orders'].values())
        delivered = [o for o in orders if o['status'] == 'delivered']
        return dict(metrics_scope='cumulative_run_counts_only', total=len(orders), delivered=len(delivered),
                    on_time=sum(o['receipt']['sim_time'] <= o['deadline_sim'] for o in delivered),
                    failed_count=sum(o['status'] == 'failed' for o in orders),
                    unfinished_count=sum(o['status'] not in {'delivered', 'failed'} for o in orders),
                    incident_total=len(incidents), resolved=sum(i['status'] == 'resolved' for i in incidents),
                    unresolved=sum(i['status'] == 'unresolved' for i in incidents),
                    receipts_count=sum(bool(o.get('receipt')) for o in orders),
                    token_usage=None, actual_monetary_cost=None)

    def _episode_report_paths(self, ref):
        episode = ref.get('episode')
        checksum = ref.get('sha256')
        if (ref.get('schema') != EPISODE_REPORT_SCHEMA or ref.get('run_id') != self.s['run_id']
                or type(episode) is not int or episode < 1
                or not isinstance(checksum, str) or len(checksum) != 64
                or any(c not in '0123456789abcdef' for c in checksum)
                or type(ref.get('bytes')) is not int or not 0 < ref['bytes'] <= 16 * 1024 * 1024
                or ref.get('controller_session') != self.s.get('controller_session')
                or ref.get('path') != self.s['run_id'] + '/episode-' + str(episode) + '-report.json'
                or ref.get('prepared_name') != '.episode-' + str(episode) + '-report-' + checksum + '.prepared'):
            raise ValueError('episode_report_reference_identity_invalid')
        folder = self.data_dir / self.s['run_id']
        if folder.is_symlink() or not folder.resolve().is_relative_to(self.data_dir.resolve()):
            raise ValueError('episode_report_folder_outside_project_run')
        return folder / ref['prepared_name'], folder / ('episode-' + str(episode) + '-report.json')

    def _unindexed_episode_originals(self):
        folder = self.data_dir / self.s['run_id']
        if folder.is_symlink() or not folder.resolve().is_relative_to(self.data_dir.resolve()):
            raise ValueError('episode_report_folder_outside_project_run')
        if not folder.exists():
            return []
        episode = str(self.s['smart_ward']['episode'])
        prefix = '.episode-' + episode + '-report-'
        final_name = 'episode-' + episode + '-report.json'
        return [path.name for path in folder.iterdir()
                if path.name == final_name or path.name.startswith(prefix) and path.name.endswith('.prepared')]

    def _finish_episode_report_seal(self, ref):
        prepared, target = self._episode_report_paths(ref)
        _publish_report_bytes(prepared, target, ref['sha256'], ref['bytes'])
        with self._atomic():
            ref.update(status='sealed')
            ref.pop('error', None)
            self.s['smart_ward']['report_seal_pending'] = True

    def _check_latest_episode_report_owner(self):
        path = self.data_dir / self.s['run_id'] / 'report.json'
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink() or not path.is_file():
            raise ValueError('latest_report_not_regular')
        raw = path.read_bytes()
        previous = [r for r in self.s['smart_ward'].get('episode_reports', {}).values()
                    if r.get('status') == 'sealed' and r.get('sha256') == hashlib.sha256(raw).hexdigest()]
        if not previous or json.loads(raw).get('report_schema') != EPISODE_REPORT_SCHEMA:
            raise ValueError('legacy_or_unowned_report_requires_new_run_no_migration')

    def _publish_latest_episode_report(self, ref):
        _, target = self._episode_report_paths(ref)
        raw = _read_report_bytes(target, ref['sha256'], ref['bytes'])
        path = target.parent / 'report.json'
        if not path.exists() or path.is_symlink() or path.read_bytes() != raw:
            self._check_latest_episode_report_owner()
            _latest_report_bytes(path, raw)
        if path.read_bytes() != raw:
            raise ValueError('latest_episode_report_publish_not_confirmed')
        with self._atomic():
            self.s['smart_ward']['latest_episode_report'] = copy.deepcopy(ref['receipt']['report_ref'])
            self.s['smart_ward']['report_seal_pending'] = False
            self.s['smart_ward'].pop('report_seal_error', None)

    def verify(self, run_id=None):
        report = super().verify(run_id)
        if self._ward_ready:
            report['smart_ward'] = self.verify_services(finalize=False)
            report['protocol'] = 'smart-ward-v1'
            orders = self._episode_orders()
            delivered = [o for o in orders if o['status'] == 'delivered']
            report.update(report_schema=EPISODE_REPORT_SCHEMA,
                episode=self.s['smart_ward']['episode'], metrics_scope='current_smart_ward_episode',
                order_ids=[o['order_id'] for o in orders], total=len(orders), delivered=len(delivered),
                on_time=sum(o['receipt']['sim_time'] <= o['deadline_sim'] for o in delivered),
                failed=[o['order_id'] for o in orders if o['status'] == 'failed'],
                unfinished=[o['order_id'] for o in orders if o['status'] not in {'delivered', 'failed'}],
                receipts=[copy.deepcopy(o['receipt']) for o in delivered],
                error_deliveries=sum(r['package_id'] != o['package_id'] or r['station'] != o['dropoff_station']
                    or math.hypot(r['arrival_pose']['x'] - self.stations[r['station']]['x'],
                                  r['arrival_pose']['y'] - self.stations[r['station']]['y']) > .5
                    for o in delivered for r in [o['receipt']]),
                run_totals=self._report_run_totals(), **self._episode_measurements())
        return report

    def reset(self):
        with self.lock:
            if self.s['smart_ward'].get('navigation_recovery_required'):
                raise ValueError('prior_navigation_stop_requires_reconciliation')
            if any(i['status'] in {'pending', 'unknown'} and i['command']['kind'] == 'handoff' for i in self.s['smart_ward']['operations'].values()):
                raise ValueError('pending_handoff_must_be_reconciled_before_reset')
            qualification = self.s['smart_ward'].get('fault_qualification')
            if qualification:
                # Preserve the old stop/latch identity until the scene actually
                # restores its controls. Recent pre-pause stationary telemetry
                # cannot authorize discarding a still-paused qualification.
                if qualification.get('active'):
                    raise ValueError('fault_qualification_must_end_before_reset')
                self._read_scene()
                try:
                    restored = json.loads((self.scene_dir / 'fault-state.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    raise ValueError('fault_qualification_restoration_not_confirmed_before_reset')
                if not isinstance(restored, dict):
                    raise ValueError('fault_qualification_restoration_not_confirmed_before_reset')
                identity = ('run_id', 'epoch', 'qualification_id', 'session_key')
                scene = self.s['smart_ward'].get('scene') or {}
                ended = qualification.get('ended_wall')
                sequence, ended_sequence = scene.get('seq'), qualification.get('ended_scene_seq')
                if (any(restored.get(key) != qualification.get(key) for key in identity)
                        or restored.get('active') is not False or restored.get('paused') or restored.get('holds')
                        or isinstance(ended, bool) or not isinstance(ended, (int, float)) or not math.isfinite(ended)
                        or not self._scene_fresh() or scene.get('wall_time', 0) <= ended
                        or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1
                        or isinstance(ended_sequence, int) and sequence <= ended_sequence
                        or not self.stopped() or not self.fresh()):
                    raise ValueError('fault_qualification_restoration_not_confirmed_before_reset')
            result = super().reset()
            self.s['protocol'] = 'smart-ward-v1'
            self._seen_scene_seq = None
            self._low_samples.clear()
            self._good_samples.clear()
            self._control()
            self.save()
            return self.state()
