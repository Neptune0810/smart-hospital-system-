"""Opt-in, bounded timings for the project-local Isaac performance trial.

The helper never changes simulation, navigation, or business state.  All spans
are inclusive and may overlap; their totals must not be added together.
"""

from __future__ import annotations

from collections import deque
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid


RUN_ID = re.compile(r"perf230-[A-Za-z0-9_-]{1,96}\Z")
OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
METRIC_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
VARIANTS = frozenset(("baseline", "camera_cache"))
PHASES = frozenset(("A1", "B1", "A2", "B2", "final"))
MAX_SAMPLES = 2048
MAX_METRICS = 64
MAX_OPERATIONS = 128
MAX_CONTROL_BYTES = 8192
PUBLISH_INTERVAL = 1.0
CONTROL_INTERVAL = 0.5


class _NullSpan:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


_NULL_SPAN = _NullSpan()


class _TimedSpan:
    def __init__(self, performance, name):
        self.performance = performance
        self.name = name

    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.performance.record_duration(self.name, time.perf_counter() - self.started)
        return False


class _Metric:
    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.maximum = 0.0
        self.samples = deque(maxlen=MAX_SAMPLES)

    def add(self, seconds):
        self.count += 1
        self.total += seconds
        self.maximum = max(self.maximum, seconds)
        self.samples.append(seconds)

    def summary(self):
        samples = sorted(self.samples)
        rank = max(0, math.ceil(0.95 * len(samples)) - 1)
        return {
            "count": self.count,
            "sum_seconds": self.total,
            "max_seconds": self.maximum,
            "p95_seconds": samples[rank],
            "p95_sample_count": len(samples),
            "p95_sample_scope": "last_2048_observations_per_interval",
        }


def _regular_directory(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"performance directory is not a regular directory: {path}")


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _open_regular(path, flags):
    if path.is_symlink():
        raise ValueError(f"performance file must not be a symlink: {path.name}")
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"performance file must be regular: {path.name}")
    return descriptor


class SimPerformance:
    """Measure one exclusive trial directory; disabled mode performs no I/O."""

    def __init__(self, root, default_variant="baseline"):
        if default_variant not in VARIANTS:
            raise ValueError("unknown default performance variant")
        run_id = os.environ.get("HOSPITAL_PERF_RUN_ID")
        self.enabled = bool(run_id)
        self.directory = None
        self._variant = default_variant
        self._phase = "initial"
        if not self.enabled:
            return
        if RUN_ID.fullmatch(run_id) is None:
            raise ValueError("invalid HOSPITAL_PERF_RUN_ID")

        self._lock = threading.RLock()
        self._root = Path(root).absolute()
        _regular_directory(self._root)
        parent = self._root
        for component in ("runs", "performance-v1"):
            parent = parent / component
            try:
                parent.mkdir()
            except FileExistsError:
                pass
            _regular_directory(parent)
        directory = parent / run_id
        directory.mkdir()  # Deliberately exclusive: never reuse or overwrite a run.
        _regular_directory(directory)
        self.directory = directory
        self.run_id = run_id
        self._metrics = {}
        self._operations = {}
        self._control_ack = None
        self._loop_count = 0
        self._published_loop_count = 0
        self._sequence = 0
        self._started = time.monotonic()
        self._interval_started = self._started
        self._last_publish = None
        self._last_poll = None
        self._last_sim_time = None
        self._last_control_content = None
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "kind": "isaac_project_performance",
            "pid": os.getpid(),
            "created_wall_time": time.time(),
            "created_monotonic": self._started,
            "default_variant": default_variant,
            "publish_interval_wall_seconds": PUBLISH_INTERVAL,
            "control_poll_interval_wall_seconds": CONTROL_INTERVAL,
            "max_metric_names": MAX_METRICS,
            "max_samples_per_metric_per_interval": MAX_SAMPLES,
            "timing_semantics": "inclusive spans may overlap; do not sum their totals",
            "loop_count_semantics": "number of publish invocations, not physics steps",
            "control_only_changes": ["performance variant", "performance phase"],
        }
        self._write_exclusive(directory / "manifest.json", manifest)
        self._write_exclusive(directory / "profile.jsonl", None)
        self._write_status()

    def _check_directory(self):
        for path in (self._root, self._root / "runs", self.directory.parent, self.directory):
            _regular_directory(path)

    def _write_exclusive(self, path, value):
        descriptor = _open_regular(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            if value is not None:
                output.write(_json(value) + "\n")

    def _write_status(self, profile=None):
        self._check_directory()
        target = self.directory / "status.json"
        if target.is_symlink():
            raise ValueError("status.json must not be a symlink")
        temporary = self.directory / (".status-" + uuid.uuid4().hex + ".tmp")
        value = {
            "schema_version": 1,
            "run_id": self.run_id,
            "wall_time": time.time(),
            "monotonic": time.monotonic(),
            "phase": self._phase,
            "variant": self._variant,
            "loop_count": self._loop_count,
            "profile_sequence": self._sequence,
            "sim_time": self._last_sim_time,
            "control_ack": self._control_ack,
        }
        if profile is not None:
            value["latest_profile"] = profile
        try:
            self._write_exclusive(temporary, value)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def variant(self):
        return self._variant

    def span(self, name):
        if not self.enabled:
            return _NULL_SPAN
        self._validate_metric_name(name)
        return _TimedSpan(self, name)

    def call(self, name, fn, *args, **kwargs):
        if not self.enabled:
            return fn(*args, **kwargs)
        with self.span(name):
            return fn(*args, **kwargs)

    @staticmethod
    def _validate_metric_name(name):
        if not isinstance(name, str) or METRIC_NAME.fullmatch(name) is None:
            raise ValueError("invalid performance metric name")

    def record_duration(self, name, seconds):
        if not self.enabled:
            return
        self._validate_metric_name(name)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("performance duration must be a finite nonnegative number")
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("performance duration must be a finite nonnegative number")
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                if len(self._metrics) >= MAX_METRICS:
                    raise ValueError("performance metric-name limit reached")
                metric = self._metrics[name] = _Metric()
            metric.add(seconds)

    def poll_control(self):
        if not self.enabled:
            return None
        now = time.monotonic()
        with self._lock:
            if self._last_poll is not None and now - self._last_poll < CONTROL_INTERVAL:
                return None
            self._last_poll = now
            control_path = self.directory / "control.json"
            self._check_directory()
            try:
                descriptor = _open_regular(control_path, os.O_RDONLY)
            except FileNotFoundError:
                return None
            except (ValueError, OSError) as error:
                return self._reject_control(str(error))
            try:
                with os.fdopen(descriptor, "rb") as source:
                    content = source.read(MAX_CONTROL_BYTES + 1)
                if content == self._last_control_content:
                    return dict(self._control_ack) if self._control_ack else None
                if len(content) > MAX_CONTROL_BYTES:
                    raise ValueError("control.json exceeds byte limit")
                value = json.loads(content.decode("utf-8"))
                if not isinstance(value, dict) or set(value) != {"operation_id", "phase", "variant"}:
                    raise ValueError("control requires only operation_id, phase, variant")
                operation_id = value["operation_id"]
                if (not isinstance(operation_id, str) or OPERATION_ID.fullmatch(operation_id) is None
                        or ".." in operation_id):
                    raise ValueError("invalid control operation_id")
                phase, variant = value["phase"], value["variant"]
                if not isinstance(phase, str) or phase not in PHASES:
                    raise ValueError("unknown control phase")
                if not isinstance(variant, str) or variant not in VARIANTS:
                    raise ValueError("unknown control variant")
                signature = (phase, variant)
                original = self._operations.get(operation_id)
                if original is not None:
                    if original[0] != signature:
                        raise ValueError("operation_id_conflict")
                    self._last_control_content = content
                    self._control_ack = dict(original[1], replayed=True)
                    self._write_status()
                    return dict(self._control_ack)
                if len(self._operations) >= MAX_OPERATIONS:
                    raise ValueError("control operation limit reached")
                # Discard the partial old interval before changing attribution.
                # Transition/warm-up intervals are not comparison evidence.
                self._metrics.clear()
                self._interval_started = now
                self._published_loop_count = self._loop_count
                self._phase, self._variant = phase, variant
                self._control_ack = {
                    "ok": True,
                    "operation_id": operation_id,
                    "phase": phase,
                    "variant": variant,
                    "applied": True,
                    "replayed": False,
                    "applied_wall_time": time.time(),
                    "applied_monotonic": now,
                    "profile_sequence": self._sequence,
                }
                self._operations[operation_id] = (signature, dict(self._control_ack))
                self._last_control_content = content
                self._write_status()
                return dict(self._control_ack)
            except (ValueError, TypeError, UnicodeError) as error:
                self._last_control_content = content
                return self._reject_control(str(error))

    def _reject_control(self, reason):
        self._control_ack = {
            "ok": False,
            "applied": False,
            "reason": reason,
            "wall_time": time.time(),
            "monotonic": time.monotonic(),
        }
        self._write_status()
        return dict(self._control_ack)

    def publish(self, sim_time, extra=None):
        if not self.enabled:
            return None
        if isinstance(sim_time, bool) or not isinstance(sim_time, (int, float)):
            raise ValueError("simulation time must be a finite nonnegative number")
        sim_time = float(sim_time)
        if not math.isfinite(sim_time) or sim_time < 0:
            raise ValueError("simulation time must be a finite nonnegative number")
        if extra is None:
            extra = {}
        if not isinstance(extra, dict):
            raise ValueError("extra performance observations must be a dictionary")
        _json(extra)  # Reject non-finite or unserializable observations before resetting metrics.
        now = time.monotonic()
        with self._lock:
            self._loop_count += 1
            self._last_sim_time = sim_time
            if self._last_publish is not None and now - self._last_publish < PUBLISH_INTERVAL:
                return None
            self._check_directory()
            profile = {
                "schema_version": 1,
                "run_id": self.run_id,
                "sequence": self._sequence + 1,
                "wall_time": time.time(),
                "monotonic": now,
                "elapsed_wall_seconds": now - self._started,
                "interval_wall_seconds": now - self._interval_started,
                "sim_time": sim_time,
                "phase": self._phase,
                "variant": self._variant,
                "loop_count": self._loop_count,
                "interval_loop_count": self._loop_count - self._published_loop_count,
                "metrics": {name: metric.summary() for name, metric in self._metrics.items()},
                "timing_semantics": "inclusive spans may overlap; do not sum their totals",
                "extra": extra,
            }
            serialized = _json(profile) + "\n"
            descriptor = _open_regular(self.directory / "profile.jsonl", os.O_WRONLY | os.O_APPEND)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(serialized)
            self._sequence += 1
            self._last_publish = now
            self._interval_started = now
            self._published_loop_count = self._loop_count
            self._metrics.clear()
            self._write_status(profile)
            return profile
