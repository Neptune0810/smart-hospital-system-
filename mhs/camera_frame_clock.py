"""Opt-in RGB completion/timing probe for the fixed Isaac Sim 6.0.1 runtime.

Importing this module attaches nothing, changes no sensor rates, and needs no
Isaac imports. Explicit construction subscribes to render-completion events.
The default application and existing JPEG paths do not use this probe.

The low-level event/ReferenceTime pairing follows NVIDIA's v6.0.1 Camera source
(the wrapper is deprecated; this module does not instantiate that wrapper).
The mapped simulation time uses the same version-specific internal interface.
Failure of that mapping refuses a new sample; current time is not a fallback.
"""
from __future__ import annotations

from collections import Counter, deque
import copy
from fractions import Fraction
import json
import math
from numbers import Integral, Real
from pathlib import Path
import threading
import time


OFFICIAL_SOURCES = (
    "https://github.com/isaac-sim/IsaacSim/blob/v6.0.1/source/deprecated/isaacsim.sensors.camera/isaacsim/sensors/camera/camera.py#L436-L598",
    "https://docs.isaacsim.omniverse.nvidia.com/6.0.1/sensors/isaacsim_sensors_multitick_rendering.html",
)


class FrameRejected(ValueError):
    """A completion event cannot establish a fresh, valid RGB frame."""


def _finite_nonnegative(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise FrameRejected(name + "_invalid")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise FrameRejected(name + "_invalid")
    return value


def _reference_time(value):
    if not isinstance(value, dict):
        raise FrameRejected("reference_time_invalid")
    numerator = value.get("referenceTimeNumerator")
    denominator = value.get("referenceTimeDenominator")
    if (isinstance(numerator, bool) or not isinstance(numerator, Integral)
            or isinstance(denominator, bool) or not isinstance(denominator, Integral)
            or numerator < 0 or denominator <= 0):
        raise FrameRejected("reference_time_invalid")
    return Fraction(int(numerator), int(denominator))


def _product_path(product):
    path = product if isinstance(product, str) else getattr(product, "path", None)
    if path is None and callable(getattr(product, "GetPath", None)):
        path = str(product.GetPath())
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("render_product_path_required")
    return path


class _IsaacRuntime:
    """Isaac imports and version-specific API calls, kept out of pure tests."""

    def __init__(self):
        import carb.eventdispatcher
        import omni.graph.core as og
        import omni.replicator.core as rep
        import omni.syntheticdata._syntheticdata as syntheticdata
        import omni.timeline
        import omni.usd
        from isaacsim.core.simulation_manager import SimulationManager

        self._dispatcher = carb.eventdispatcher.get_eventdispatcher()
        self._context = omni.usd.get_context()
        self._event_type = omni.usd.StageRenderingEventType.NEW_FRAME
        self._controller = og.Controller()
        self._rep = rep
        self._sdg = syntheticdata.acquire_syntheticdata_interface()
        self._timeline = omni.timeline.get_timeline_interface()
        self._simulation_manager = SimulationManager
        self._references = {}
        self._observer = None
        self._graph = None

    def attach_reference(self, product_path):
        reference = self._rep.AnnotatorRegistry.get_annotator("ReferenceTime")
        self._references[product_path] = reference
        reference.attach([product_path])

    def subscribe(self, callback):
        # Attaching annotators first creates the SDG graph, as in the fixed
        # official implementation. The listener does not step the application.
        self._graph = self._controller.graph("/Render/PostProcess/SDGPipeline")
        self._observer = self._dispatcher.observe_event(
            event_name=self._context.stage_rendering_event_name(self._event_type, True),
            on_event=callback,
            observer_name="hospital.CameraFrameProbe." + str(time.time_ns()),
        )

    def event_product(self, event):
        return self._sdg.parse_rendered_simulation_event(
            event["product_path_handle"], event["results"]
        )[0]

    def playing(self):
        return bool(self._timeline.is_playing())

    def evaluate(self):
        self._controller.evaluate_sync(graph_id=self._graph)

    def reference(self, product_path):
        return self._references[product_path].get_data()

    def map_simulation_time(self, reference):
        return self._simulation_manager._simulation_manager_interface.get_simulation_time_at_time(
            (reference["referenceTimeNumerator"], reference["referenceTimeDenominator"])
        )

    def observed_simulation_time(self):
        return self._simulation_manager.get_simulation_time()

    def close(self):
        # ObserverGuard's lifetime controls the subscription, as in Camera.
        self._observer = None
        errors = []
        for product_path, reference in self._references.items():
            try:
                reference.detach([product_path])
            except Exception as error:
                errors.append({"product_path": product_path, "error": repr(error)})
        self._references.clear()
        return errors


class CameraFrameProbe:
    """Observe existing RGB products; never create products or encode JPEG.

    Example (explicit opt-in, after both RGB annotators have been attached)::

        probe = CameraFrameProbe(
            {"third_person": (rp, rgb), "first_person": (fp_rp, fp_rgb)},
            output_dir=unique_acceptance_directory / "camera-clock",
        )
        # Keep the existing full app.update() loop unchanged.
        frame = probe.latest("first_person", max_age_seconds=1.5)
        # frame is {"rgb": copied_array, "metadata": ...}, or None.
        probe.close()

    The output directory must be new, so a later probe cannot overwrite an
    earlier result. callback_wall_time is when this process observed completion,
    not an invented GPU exposure wall-clock timestamp. reference_time and
    mapped_sim_time preserve the renderer/physics association separately.
    """

    def __init__(self, sources, *, output_dir, performance=None, _runtime=None,
                 _wall_clock=time.time, _monotonic_clock=time.monotonic):
        if not sources:
            raise ValueError("at_least_one_existing_rgb_product_required")
        self._performance = performance
        self._sources = {}
        self._by_path = {}
        for name, (product, annotator) in sources.items():
            if not isinstance(name, str) or not name:
                raise ValueError("source_name_required")
            path = _product_path(product)
            if path in self._by_path:
                raise ValueError("independent_sources_require_distinct_products")
            if not callable(getattr(annotator, "get_data", None)):
                raise ValueError("existing_rgb_annotator_required")
            self._sources[name] = {"path": path, "annotator": annotator,
                                   "reference": None, "sim_time": None, "seq": 0,
                                   "latest": None, "awaiting_resume_frame": False}
            self._by_path[path] = name
        self._lock = threading.RLock()
        self._wall_clock = _wall_clock
        self._monotonic_clock = _monotonic_clock
        self._counts = Counter()
        self._diagnostics = deque(maxlen=64)
        self._diagnostic_fingerprints = set()
        self._closed = False
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self._write_json("manifest.json", {
            "schema_version": 1, "target_isaac_sim_version": "6.0.1",
            "created_wall_time": self._wall_clock(),
            "sources": {name: source["path"] for name, source in self._sources.items()},
            "sensor_rates_modified_by_constructor": False,
            "official_sources": list(OFFICIAL_SOURCES),
        })
        self._runtime = None
        try:
            self._runtime = _runtime if _runtime is not None else _IsaacRuntime()
            for source in self._sources.values():
                self._runtime.attach_reference(source["path"])
            self._runtime.subscribe(self._on_render)
        except Exception as error:
            self._reject("initialization_error", error=error)
            try:
                if self._runtime is not None:
                    self._runtime.close()
            except Exception:
                pass
            raise

    def _write_json(self, filename, payload):
        with (self.output_dir / filename).open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, allow_nan=False)

    def _append(self, filename, payload):
        with (self.output_dir / filename).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, allow_nan=False) + "\n")

    def _reject(self, reason, name=None, error=None):
        self._counts[reason] += 1
        detail = {"reason": reason, "source": name, "wall_time": self._wall_clock()}
        if error is not None:
            detail.update(error_type=type(error).__name__, error=str(error))
        self._diagnostics.append(detail)
        # Repeated skipped frames are counted, rather than flooding disk.
        fingerprint = (reason, name, detail.get("error_type"), detail.get("error"))
        if fingerprint not in self._diagnostic_fingerprints:
            self._diagnostic_fingerprints.add(fingerprint)
            try:
                self._append("diagnostics.jsonl", detail)
            except Exception:
                self._counts["diagnostic_write_failed"] += 1

    def _on_render(self, event):
        if self._performance is None:
            return self._capture_on_render(event)
        # Includes SDG evaluation, readback, validation, copying and persistence;
        # this is nested inside app_update when completion events are dispatched.
        with self._performance.span("camera_callback"):
            return self._capture_on_render(event)

    def _capture_on_render(self, event):
        with self._lock:
            if self._closed:
                return
            name = None
            try:
                callback_wall = _finite_nonnegative(self._wall_clock(), "callback_wall_time")
                callback_mono = _finite_nonnegative(self._monotonic_clock(), "callback_monotonic")
                path = self._runtime.event_product(event)
                name = self._by_path.get(path)
                if name is None:
                    self._counts["other_product"] += 1
                    return
                if not self._runtime.playing():
                    for item in self._sources.values():
                        item["awaiting_resume_frame"] = True
                    raise FrameRejected("simulation_not_playing")
                source = self._sources[name]
                self._runtime.evaluate()
                raw_reference = self._runtime.reference(path)
                reference = _reference_time(raw_reference)
                if source["reference"] is not None and reference <= source["reference"]:
                    raise FrameRejected("reference_not_advanced")
                mapped_sim = _finite_nonnegative(
                    self._runtime.map_simulation_time(raw_reference), "mapped_sim_time")
                observed_sim = _finite_nonnegative(
                    self._runtime.observed_simulation_time(), "observed_sim_time")
                if mapped_sim > observed_sim + 1e-6:
                    raise FrameRejected("mapped_sim_time_in_future")
                if source["sim_time"] is not None and mapped_sim <= source["sim_time"]:
                    raise FrameRejected("physics_time_not_advanced")
                import numpy as np
                raw_rgb = source["annotator"].get_data()
                if raw_rgb is None:
                    raise FrameRejected("rgb_unavailable")
                rgb = np.array(raw_rgb, copy=True)
                if (rgb.ndim != 3 or rgb.shape[0] == 0 or rgb.shape[1] == 0
                        or rgb.shape[2] not in (3, 4) or not np.issubdtype(rgb.dtype, np.number)
                        or not np.isfinite(rgb).all()):
                    raise FrameRejected("rgb_invalid")
                if _reference_time(self._runtime.reference(path)) != reference:
                    raise FrameRejected("reference_changed_during_rgb_read")
                # Reject a pause/stop that occurred while reading annotators.
                if not self._runtime.playing():
                    for item in self._sources.values():
                        item["awaiting_resume_frame"] = True
                    raise FrameRejected("simulation_not_playing")
                metadata = {
                    "schema_version": 1, "source": name, "render_product_path": path,
                    "completed_seq": source["seq"] + 1,
                    "reference_time": {"numerator": reference.numerator,
                                       "denominator": reference.denominator},
                    "raw_reference_time": {key: int(raw_reference[key]) for key in
                                           ("referenceTimeNumerator", "referenceTimeDenominator")},
                    "callback_wall_time": callback_wall,
                    "callback_monotonic": callback_mono,
                    "rgb_copied_wall_time": self._wall_clock(),
                    "mapped_sim_time": mapped_sim, "observed_sim_time": observed_sim,
                    "simulation_advancing": True,
                    "rgb_shape": list(rgb.shape), "rgb_dtype": str(rgb.dtype),
                }
                # Persist evidence before accepting the frame into the cache.
                # A disk or mapping failure does not stamp an old image fresh.
                self._append("frames.jsonl", metadata)
                source.update(reference=reference, sim_time=mapped_sim,
                              seq=metadata["completed_seq"],
                              awaiting_resume_frame=False,
                              latest={"rgb": rgb, "metadata": metadata})
                self._counts["completed"] += 1
            except FrameRejected as error:
                self._reject(str(error), name)
            except Exception as error:
                self._reject("capture_error", name, error)

    def latest(self, name, *, max_age_seconds=1.5):
        """Return a copy of a fresh completed sample without changing its stamp."""
        age_limit = _finite_nonnegative(max_age_seconds, "max_age_seconds")
        with self._lock:
            source = self._sources[name]
            frame = source["latest"]
            if frame is None or self._closed:
                return None
            try:
                if not self._runtime.playing():
                    for item in self._sources.values():
                        item["awaiting_resume_frame"] = True
                    return None
            except Exception as error:
                self._reject("latest_runtime_error", name, error)
                return None
            if source["awaiting_resume_frame"]:
                return None
            age = self._monotonic_clock() - frame["metadata"]["callback_monotonic"]
            if not math.isfinite(age) or age < 0 or age > age_limit:
                return None
            return {"rgb": frame["rgb"].copy(), "metadata": copy.deepcopy(frame["metadata"])}

    def status(self):
        with self._lock:
            return {"closed": self._closed, "counts": dict(self._counts),
                    "sources": {name: {"completed_seq": source["seq"],
                                       "last_frame": None if source["latest"] is None else
                                       copy.deepcopy(source["latest"]["metadata"])}
                                for name, source in self._sources.items()},
                    "recent_diagnostics": copy.deepcopy(list(self._diagnostics))}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                errors = self._runtime.close()
            except Exception as error:
                errors = [{"error_type": type(error).__name__, "error": str(error)}]
            if errors:
                self._reject("detach_failed", error=RuntimeError(repr(errors)))
            self._write_json("summary.json", self.status())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def set_camera_tick_rate(camera_path, tick_rate):
    """Explicit opt-in authoring API; never touches Lidar or creates a camera.

    Invoke only in a separately identified probe/version. This does not alter
    the physics, application loop, resolution, or any existing render product.
    """
    rate = _finite_nonnegative(tick_rate, "tick_rate")
    import omni.usd
    from isaacsim.sensors.experimental.rtx import RtxCamera

    stage = omni.usd.get_context().get_stage()
    prim = stage.GetPrimAtPath(camera_path) if stage is not None else None
    if not prim or not prim.IsValid() or prim.GetTypeName() != "Camera":
        raise ValueError("existing_camera_prim_required")
    attribute = prim.GetAttribute("omni:sensor:tickRate")
    original = attribute.Get() if attribute else None
    RtxCamera(path=camera_path, tick_rate=rate, reset_xform_op_properties=False)
    actual = prim.GetAttribute("omni:sensor:tickRate").Get()
    if actual is None or not math.isclose(float(actual), rate, rel_tol=0, abs_tol=1e-9):
        raise RuntimeError("camera_tick_rate_readback_mismatch")
    return {"camera_path": camera_path, "requested_tick_rate": rate,
            "actual_tick_rate": actual, "previous_tick_rate": original}
