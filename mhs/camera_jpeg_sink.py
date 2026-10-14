"""Publish only actual completed camera frames to the existing JPEG endpoints.

This module never reads a renderer, advances simulation, or changes a sensor.
The caller supplies CameraFrameProbe.latest() and the actual timeline state.
The original completion stamps survive JPEG encoding and publication.
"""
from __future__ import annotations

import copy
from fractions import Fraction
import hashlib
import io
import json
import math
from numbers import Integral, Real
from pathlib import Path
import threading
import time
import uuid


SOURCES = {
    "third_person": ("latest.jpg", "latest-frame.json"),
    "first_person": ("first-person.jpg", "first-person-frame.json"),
}
MAX_CALLBACK_AGE_SECONDS = .35
MIN_PUBLICATION_INTERVAL_SECONDS = 1.0


class _Refused(ValueError):
    pass


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _Refused(name + "_invalid")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise _Refused(name + "_invalid")
    return value


def _reference(value):
    if not isinstance(value, dict):
        raise _Refused("reference_time_invalid")
    numerator, denominator = value.get("numerator"), value.get("denominator")
    if (isinstance(numerator, bool) or not isinstance(numerator, Integral)
            or isinstance(denominator, bool) or not isinstance(denominator, Integral)
            or numerator < 0 or denominator <= 0):
        raise _Refused("reference_time_invalid")
    return Fraction(int(numerator), int(denominator))


class CameraJpegSink:
    """One writer for two known views, with independent 1 Hz wall limits.

    ``publish(view, frame, camera=..., simulation_playing=...)`` returns a
    dictionary with ``written`` and ``reason``. A successful third-person
    publication also returns its PIL ``image`` for the caller's existing
    recorder. No recording is performed here. ``frame=None`` and a paused
    timeline never touch the existing image or its metadata.

    Construct once per simulation process. Concurrent sink instances targeting
    the same directory are not supported; the application owns this writer.
    JPEG and metadata are replaced individually. Existing SHA-256 readers
    reject the short pair-replacement window instead of accepting mixed data.
    """

    def __init__(self, root, *, _wall_clock=time.time,
                 _monotonic_clock=time.monotonic):
        self.directory = Path(root) / "runs"
        self._wall_clock = _wall_clock
        self._monotonic_clock = _monotonic_clock
        self._last = {}
        self._seen = {}
        self._lock = threading.RLock()
        self._temporary_id = uuid.uuid4().hex

    def should_publish(self, view, now):
        """Read-only publication hint; ``now`` is monotonic seconds.

        The caller and this sink must share the same monotonic clock domain.
        This does not reserve a slot or inspect/stamp a camera frame. publish()
        still checks its own clock, freshness and rate under the existing lock.
        Multiple callers can observe True; only a successful publish consumes
        the per-view interval. The application remains the single writer.
        """
        if view not in SOURCES:
            raise ValueError("unknown_camera_view")
        monotonic = _number(now, "monotonic_time")
        with self._lock:
            previous = self._last.get(view)
            return (previous is None or
                    monotonic - previous["published_monotonic"] >= MIN_PUBLICATION_INTERVAL_SECONDS)

    def _fresh(self, meta):
        wall = _number(self._wall_clock(), "wall_time")
        monotonic = _number(self._monotonic_clock(), "monotonic_time")
        wall_age = wall - _number(meta.get("callback_wall_time"), "callback_wall_time")
        monotonic_age = monotonic - _number(meta.get("callback_monotonic"), "callback_monotonic")
        if not (0 <= wall_age <= MAX_CALLBACK_AGE_SECONDS
                and 0 <= monotonic_age <= MAX_CALLBACK_AGE_SECONDS):
            raise _Refused("callback_stale_or_future")
        return wall, monotonic

    def publish(self, view, frame, *, camera, simulation_playing):
        """Encode a fresh, previously unpublished completion, or explain refusal."""
        if view not in SOURCES:
            raise ValueError("unknown_camera_view")
        result = {"written": False, "view": view}
        with self._lock:
            temporary_paths = []
            try:
                if simulation_playing is not True:
                    raise _Refused("simulation_not_playing")
                if frame is None:
                    raise _Refused("no_completed_frame")
                if not isinstance(frame, dict) or not isinstance(frame.get("metadata"), dict):
                    raise _Refused("completed_frame_invalid")
                meta = copy.deepcopy(frame["metadata"])
                if meta.get("source") != view or meta.get("simulation_advancing") is not True:
                    raise _Refused("completed_frame_source_or_progress_invalid")
                path = meta.get("render_product_path")
                if not isinstance(path, str) or not path.startswith("/"):
                    raise _Refused("render_product_path_invalid")
                seq = meta.get("completed_seq")
                if isinstance(seq, bool) or not isinstance(seq, Integral) or seq < 1:
                    raise _Refused("completed_seq_invalid")
                seq = int(seq)
                reference = _reference(meta.get("reference_time"))
                simulation_time = _number(meta.get("mapped_sim_time"), "mapped_sim_time")
                observed_time = _number(meta.get("observed_sim_time"), "observed_sim_time")
                if simulation_time > observed_time + 1e-6:
                    raise _Refused("mapped_sim_time_in_future")
                _, monotonic = self._fresh(meta)
                identity = (seq, path, reference, simulation_time,
                            meta["callback_wall_time"], meta["callback_monotonic"])
                seen = self._seen.get(view)
                if seen and seq < seen[0]:
                    raise _Refused("completed_seq_out_of_order")
                if seen and seq == seen[0] and identity != seen:
                    raise _Refused("completed_seq_reused_with_changed_frame")
                previous = self._last.get(view)
                if previous:
                    if seq <= previous["seq"]:
                        raise _Refused("completed_seq_not_advanced")
                    if path != previous["path"]:
                        raise _Refused("render_product_changed")
                    if reference <= previous["reference"]:
                        raise _Refused("reference_time_not_advanced")
                    if simulation_time <= previous["sim_time"]:
                        raise _Refused("mapped_sim_time_not_advanced")
                if any(other != view and prior["path"] == path
                       for other, prior in self._last.items()):
                    raise _Refused("render_product_shared_between_views")

                import numpy as np
                from PIL import Image
                data = frame.get("rgb")
                if (not isinstance(data, np.ndarray) or data.dtype != np.uint8
                        or data.ndim != 3 or not data.shape[0] or not data.shape[1]
                        or data.shape[2] not in (3, 4)):
                    raise _Refused("completed_rgb_invalid")
                # A newer completion supersedes an older one even when the
                # publication rate limit skips it. An identical unpublished
                # completion may still be retried after an I/O failure.
                self._seen[view] = identity
                if previous and monotonic - previous["published_monotonic"] < MIN_PUBLICATION_INTERVAL_SECONDS:
                    raise _Refused("wall_rate_limited")
                image = Image.fromarray(np.array(data[:, :, :3], copy=True))
                encoded = io.BytesIO()
                image.save(encoded, format="JPEG", quality=82)
                raw = encoded.getvalue()
                encoded_wall, _ = self._fresh(meta)
                output = {
                    "capture_wall_time": meta["callback_wall_time"],
                    "sim_time": meta["mapped_sim_time"],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "width": image.width, "height": image.height, "view": view,
                    "camera": copy.deepcopy(camera),
                    "completed_seq": seq, "reference_time": copy.deepcopy(meta["reference_time"]),
                    "encoded_wall_time": encoded_wall,
                    "frame_clock": meta,
                    "timestamp_source": "render_completion_callback_and_reference_time",
                }
                # Reject non-JSON camera metadata before any publication.
                payload = json.dumps(output, allow_nan=False)
                self.directory.mkdir(parents=True, exist_ok=True)
                image_name, metadata_name = SOURCES[view]
                image_temp = self.directory / (image_name + ".sink-" + self._temporary_id + ".tmp")
                metadata_temp = self.directory / (metadata_name + ".sink-" + self._temporary_id + ".tmp")
                temporary_paths = [image_temp, metadata_temp]
                image_temp.write_bytes(raw)
                metadata_temp.write_text(payload, encoding="utf-8")
                # Encoding or disk staging can itself exceed the strict age.
                # Check again before replacing either published endpoint.
                _, published_monotonic = self._fresh(meta)
                image_temp.replace(self.directory / image_name)
                metadata_temp.replace(self.directory / metadata_name)
                self._last[view] = {"seq": seq, "reference": reference,
                                    "sim_time": simulation_time, "path": path,
                                    "published_monotonic": published_monotonic}
                result.update(written=True, reason="published", metadata=output)
                if view == "third_person":
                    result["image"] = image
                return result
            except _Refused as error:
                result["reason"] = str(error)
            except Exception as error:
                result.update(reason="publication_error", error_type=type(error).__name__, error=str(error))
            finally:
                for path in temporary_paths:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        # Cleanup failure does not turn a refused/stale frame
                        # into a publication or advance the completion cache.
                        pass
            return result
