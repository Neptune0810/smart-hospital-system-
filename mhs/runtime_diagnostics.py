"""Bounded asynchronous timing evidence; never a source of motion authority.

No project state is persisted or read here. Observed freshness results come from
the original method calls, never a second freshness calculation or file read.
Writer failure/queue overflow cannot block a callback or change its result.
"""
from contextlib import contextmanager
import functools
import itertools
import json
import math
from pathlib import Path
import queue
import re
import threading
import time
import uuid


SCHEMA = 'hospital-runtime-diagnostics-v1'
SLOW_SECONDS = .2
SUMMARY_SECONDS = 5
_RUN_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


def _bounded(value, depth=0):
    """Small, detached JSON values only; never traverse a full state object."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (float, int)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:500]
    if depth >= 4:
        return '<depth_limit>'
    if isinstance(value, dict):
        return {str(key)[:80]: _bounded(item, depth+1)
                for key, item in itertools.islice(value.items(), 32)}
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth+1) for item in value[:16]]
    return '<unsupported_type:' + type(value).__name__[:60] + '>'


class RuntimeDiagnostics:
    def __init__(self, directory, run_id_getter, snapshot_getter=None, *,
                 queue_size=256, wall_clock=time.time, clock=time.monotonic,
                 start=True, summary_seconds=SUMMARY_SECONDS,
                 max_written_bytes=32*1024*1024):
        if type(queue_size) is not int or not 1 <= queue_size <= 4096:
            raise ValueError('diagnostic_queue_size_out_of_bounds')
        if not .1 <= summary_seconds <= 60:
            raise ValueError('diagnostic_summary_period_out_of_bounds')
        if type(max_written_bytes) is not int or not 1 <= max_written_bytes <= 64*1024*1024:
            raise ValueError('diagnostic_file_budget_out_of_bounds')
        self.directory = Path(directory).resolve()
        self.run_id_getter, self.snapshot_getter = run_id_getter, snapshot_getter
        self.wall_clock, self.clock = wall_clock, clock
        self.summary_seconds = summary_seconds
        self.max_written_bytes = max_written_bytes
        self.instance_id = uuid.uuid4().hex[:12]
        self.queue = queue.Queue(maxsize=queue_size)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.local = threading.local()
        self.latest_actual_freshness = None
        self.spans = {}
        self.counts = dict(enqueued=0, written=0, dropped=0, dropped_exception_events=0,
                           write_errors=0, snapshot_errors=0, invalid_run_ids=0,
                           written_bytes=0, bytes_budget_used=0, file_limit_dropped=0)
        self.last_write_error = None
        self.last_drop = None
        self.sequence = 0
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._writer,
                name='hospital-runtime-diagnostics', daemon=True)
            self.thread.start()

    def snapshot(self):
        if self.snapshot_getter is None:
            return {}
        try:
            return _bounded(self.snapshot_getter())
        except Exception as error:
            with self.lock:
                self.counts['snapshot_errors'] += 1
            return {'diagnostic_snapshot_error': type(error).__name__}

    def status(self):
        with self.lock:
            result = dict(self.counts, last_write_error=self.last_write_error,
                          last_drop=self.last_drop)
        result.update(enabled=True, instance_id=self.instance_id,
            queue_depth=self.queue.qsize(), queue_capacity=self.queue.maxsize,
            writer_alive=bool(self.thread and self.thread.is_alive()),
            file_budget_bytes=self.max_written_bytes,
            asynchronous_records_are_not_stop_confirmation=True)
        return result

    def record(self, event, **fields):
        try:
            run_id = self.run_id_getter()
            if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
                with self.lock:
                    self.counts['invalid_run_ids'] += 1
                return False
            with self.lock:
                self.sequence += 1
                sequence = self.sequence
            row = dict(schema=SCHEMA, instance_id=self.instance_id, seq=sequence,
                run_id=run_id, wall_time=self.wall_clock(), monotonic_time=self.clock(),
                thread_id=threading.get_ident(), event=str(event)[:80], fields=_bounded(fields))
            try:
                self.queue.put_nowait(row)
            except queue.Full:
                with self.lock:
                    self.counts['dropped'] += 1
                    if event != 'summary':
                        self.counts['dropped_exception_events'] += 1
                    self.last_drop = dict(event=str(event)[:80], wall_time=row['wall_time'])
                return False
            with self.lock:
                self.counts['enqueued'] += 1
            return True
        except Exception:
            # Diagnostics must not change original business/ROS behavior.
            with self.lock:
                self.counts['dropped'] += 1
            return False

    def note_span(self, name, seconds, error=False):
        with self.lock:
            if name not in self.spans and len(self.spans) >= 31:
                name = '<other_spans>'
            item = self.spans.setdefault(name, dict(calls=0, slow=0, errors=0,
                                                   total_seconds=0., max_seconds=0.))
            item['calls'] += 1
            item['total_seconds'] += seconds
            item['max_seconds'] = max(item['max_seconds'], seconds)
            item['slow'] += seconds >= SLOW_SECONDS
            item['errors'] += bool(error)

    def summary(self):
        with self.lock:
            spans = {name: dict(value) for name, value in self.spans.items()}
        return self.record('summary', status=self.status(), spans=spans,
            snapshot=self.snapshot(),
            latest_actual_freshness=self.latest_actual_freshness)

    def _write(self, row):
        try:
            serialized = json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n'
            length = len(serialized.encode('utf-8'))
            with self.lock:
                if self.counts['bytes_budget_used'] + length > self.max_written_bytes:
                    self.counts['file_limit_dropped'] += 1
                    return
                self.counts['bytes_budget_used'] += length
            # run_id was validated at capture time, not replaced by the latest run.
            folder = self.directory / row['run_id']
            folder.mkdir(parents=True, exist_ok=True)
            if not folder.resolve().is_relative_to(self.directory):
                raise ValueError('diagnostic_run_directory_outside_project_data')
            path = folder / ('runtime-diagnostics-' + self.instance_id + '.jsonl')
            if path.is_symlink():
                raise ValueError('diagnostic_file_must_not_be_symlink')
            with path.open('a', encoding='utf-8') as output:
                output.write(serialized)
            with self.lock:
                self.counts['written'] += 1
                self.counts['written_bytes'] += length
        except Exception as error:
            with self.lock:
                self.counts['write_errors'] += 1
                self.last_write_error = dict(type=type(error).__name__,
                    message=str(error)[:300], wall_time=self.wall_clock())

    def _writer(self):
        next_summary = self.clock() + self.summary_seconds
        while not self.stop.is_set() or not self.queue.empty():
            if not self.stop.is_set() and self.clock() >= next_summary:
                self.summary()
                next_summary = self.clock() + self.summary_seconds
            try:
                row = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                self._write(row)
            finally:
                self.queue.task_done()

    def close(self, wait_seconds=0):
        """Default returns immediately. Motion stop never calls a writer join."""
        self.stop.set()
        if wait_seconds and self.thread:
            self.thread.join(timeout=min(1, max(0, wait_seconds)))


def _instance(engine):
    return getattr(engine, '_runtime_diagnostics', None)


def record(engine, event, **fields):
    diagnostics = _instance(engine)
    if diagnostics is None:
        return False
    try:
        return diagnostics.record(event, **fields)
    except Exception:
        return False


def snapshot(engine):
    diagnostics = _instance(engine)
    return diagnostics.snapshot() if diagnostics else {}


def status(engine):
    diagnostics = _instance(engine)
    try:
        return diagnostics.status() if diagnostics else {'enabled': False}
    except Exception:
        return {'enabled': False, 'diagnostic_status_error': True}


def actual_freshness(engine):
    diagnostics = _instance(engine)
    if diagnostics is None:
        return None
    return _bounded(getattr(diagnostics.local, 'latest_freshness', None)
                    or diagnostics.latest_actual_freshness)


@contextmanager
def trace_span(engine, name, **fields):
    diagnostics = _instance(engine)
    if diagnostics is None:
        yield
        return
    try:
        started = diagnostics.clock()
    except Exception:
        yield
        return
    error_type = None
    try:
        yield
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        try:
            seconds = max(0., diagnostics.clock() - started)
            diagnostics.note_span(name, seconds, bool(error_type))
            if seconds >= SLOW_SECONDS or error_type:
                diagnostics.record('span_error' if error_type else 'slow_span',
                    span=name, seconds=seconds, error_type=error_type,
                    snapshot=diagnostics.snapshot(), **fields)
        except Exception:
            pass


def _observe_return(diagnostics, name, original, select):
    @functools.wraps(original)
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        try:
            value = select(result)
            value['observed_wall_time'] = diagnostics.wall_clock()
            context = getattr(diagnostics.local, 'fresh_context', None)
            if context is not None:
                context[name] = value
        except Exception:
            pass
        return result
    return observed


def configure(engine, snapshot_getter=None, *, queue_size=256):
    """Enable once at navigation startup; retain original freshness/stop calls."""
    existing = _instance(engine)
    if existing:
        if snapshot_getter is not None:
            existing.snapshot_getter = snapshot_getter
        return existing
    root = Path(engine.root).resolve()
    directory = Path(engine.data_dir).resolve()
    if not directory.is_relative_to(root / 'runs'):
        raise ValueError('diagnostic_directory_must_be_project_run_data')
    diagnostics = RuntimeDiagnostics(directory, lambda: engine.s['run_id'],
        snapshot_getter, queue_size=queue_size)
    engine._runtime_diagnostics = diagnostics
    # These wrappers observe the actual values used by fresh(), without a
    # second read/call. They do not alter the returned object or exception.
    original_physical = engine.physical_pose
    engine.physical_pose = _observe_return(diagnostics, 'physical_pose', original_physical,
        lambda result: dict(returned_available=result is not None,
            producer_wall_time=result.get('wall_time') if isinstance(result, dict) else None,
            physics_time=result.get('physics_time') if isinstance(result, dict) else None))
    routes = getattr(engine, 'routes', None)
    if routes is not None:
        routes.crowd = _observe_return(diagnostics, 'crowd', routes.crowd,
            lambda result: dict(returned_fresh=result.get('fresh'),
                producer_wall_time=result.get('wall_time'), sim_time=result.get('sim_time'),
                people_count=len(result.get('people', []))))
    original_fresh = engine.fresh
    @functools.wraps(original_fresh)
    def observed_fresh(*args, **kwargs):
        prior = getattr(diagnostics.local, 'fresh_context', None)
        context = {}
        diagnostics.local.fresh_context = context
        try:
            result = original_fresh(*args, **kwargs)
            try:
                context.update(returned_fresh=bool(result), observed_wall_time=diagnostics.wall_clock(),
                    thread_id=threading.get_ident(), pose_wall_time=getattr(engine, 'last_pose_wall', None))
                detached = _bounded(context)
                diagnostics.local.latest_freshness = detached
                diagnostics.latest_actual_freshness = detached
            except Exception:
                pass
            return result
        finally:
            diagnostics.local.fresh_context = prior
    engine.fresh = observed_fresh
    original_trip = engine.guard.trip
    @functools.wraps(original_trip)
    def observed_trip(reason):
        was_armed = engine.guard.armed
        evidence, checked = None, None
        try:
            evidence = diagnostics.snapshot() if was_armed else None
            checked = actual_freshness(engine) if was_armed else None
        except Exception:
            pass
        try:
            return original_trip(reason)
        finally:
            if was_armed:
                try:
                    record(engine, 'guard_trip', reason=reason, was_armed=True,
                        snapshot_before_trip=evidence, actual_freshness=checked)
                except Exception:
                    pass
    engine.guard.trip = observed_trip
    diagnostics.record('diagnostics_started', queue_capacity=queue_size,
        slow_threshold_seconds=SLOW_SECONDS, summary_seconds=SUMMARY_SECONDS,
        observational_wrappers=True, no_executor_or_safety_threshold_changes=True)
    return diagnostics
