"""Camera clock boundary regressions; no Isaac, GPU, network, or image fixture."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np

from .camera_frame_clock import CameraFrameProbe, set_camera_tick_rate


class Clock:
    def __init__(self):
        self.wall = 1000.0
        self.mono = 100.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class Product:
    def __init__(self, path):
        self.path = path


class RGB:
    def __init__(self):
        self.value = np.full((3, 4, 3), 23, dtype=np.uint8)
        self.error = None
        self.after_read = None

    def get_data(self):
        if self.error:
            raise self.error
        if self.after_read:
            self.after_read()
        return self.value


class Runtime:
    def __init__(self):
        self.is_playing = True
        self.bound = []
        self.references = {}
        self.mapping = {}
        self.observed = 1.0
        self.mapping_error = None
        self.playing_error = None
        self.evaluations = 0
        self.closed = False

    def sample(self, path, numerator, denominator, sim_time):
        self.references[path] = {"referenceTimeNumerator": numerator,
                                 "referenceTimeDenominator": denominator}
        self.mapping[(numerator, denominator)] = sim_time
        self.observed = max(self.observed, sim_time)

    def attach_reference(self, path):
        self.bound.append(path)

    def subscribe(self, callback):
        self.callback = callback

    def event_product(self, event):
        return event["product"]

    def playing(self):
        if self.playing_error:
            raise self.playing_error
        return self.is_playing

    def evaluate(self):
        self.evaluations += 1

    def reference(self, path):
        return self.references[path]

    def map_simulation_time(self, reference):
        if self.mapping_error:
            raise self.mapping_error
        return self.mapping[(reference["referenceTimeNumerator"],
                             reference["referenceTimeDenominator"])]

    def observed_simulation_time(self):
        return self.observed

    def emit(self, path):
        self.callback({"product": path})

    def close(self):
        self.closed = True
        return []


class CameraFrameClockTests(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1] / ".cache/camera-clock-tests"
        cache.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="logic-", dir=cache)
        self.clock = Clock()
        self.runtime = Runtime()
        self.rgb = {name: RGB() for name in ("third", "first")}
        self.paths = {"third": "/Render/third", "first": "/Render/first"}
        self.output = Path(self.temporary.name) / "probe"
        self.probe = CameraFrameProbe(
            {"third": (Product(self.paths["third"]), self.rgb["third"]),
             "first": (self.paths["first"], self.rgb["first"])},
            output_dir=self.output, _runtime=self.runtime,
            _wall_clock=lambda: self.clock.wall,
            _monotonic_clock=lambda: self.clock.mono,
        )

    def tearDown(self):
        self.probe.close()
        self.temporary.cleanup()

    def emit(self, name="third", numerator=1, denominator=5, sim_time=0.2):
        path = self.paths[name]
        self.runtime.sample(path, numerator, denominator, sim_time)
        self.runtime.emit(path)

    def test_two_products_bind_independently_and_never_change_rate(self):
        self.assertEqual(self.runtime.bound, list(self.paths.values()))
        self.emit("third")
        self.assertIsNone(self.probe.latest("first"))
        self.emit("first")
        self.assertEqual(self.probe.latest("first")["metadata"]["completed_seq"], 1)
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertFalse(manifest["sensor_rates_modified_by_constructor"])

    def test_other_product_event_does_not_read_or_stamp_rgb(self):
        self.runtime.emit("/Render/Lidar")
        self.assertEqual(self.runtime.evaluations, 0)
        self.assertIsNone(self.probe.latest("third"))
        self.assertEqual(self.probe.status()["counts"]["other_product"], 1)

    def test_equal_or_older_rational_time_preserves_original_stamp(self):
        self.emit()
        original = self.probe.latest("third")["metadata"]
        self.clock.advance(0.3)
        self.emit(numerator=2, denominator=10)
        self.emit(numerator=1, denominator=10, sim_time=0.1)
        self.assertEqual(self.probe.latest("third")["metadata"], original)
        self.assertEqual(self.probe.status()["counts"]["reference_not_advanced"], 2)
        self.assertEqual(len((self.output / "frames.jsonl").read_text().splitlines()), 1)

    def test_pause_never_refreshes_and_resume_requires_a_new_sample(self):
        self.emit()
        original = self.probe.status()["sources"]["third"]["last_frame"]
        self.clock.advance(0.1)
        self.runtime.is_playing = False
        self.emit(numerator=2, sim_time=0.4)
        self.assertIsNone(self.probe.latest("third"))
        self.assertEqual(self.probe.status()["sources"]["third"]["last_frame"], original)
        self.runtime.is_playing = True
        self.assertIsNone(self.probe.latest("third"))
        self.emit(numerator=2, sim_time=0.4)
        self.assertEqual(self.probe.latest("third")["metadata"]["completed_seq"], 2)

    def test_new_reference_without_physics_progress_is_rejected(self):
        self.emit()
        self.clock.advance(0.1)
        self.emit(numerator=2, sim_time=0.2)
        self.assertEqual(self.probe.latest("third")["metadata"]["completed_seq"], 1)
        self.assertEqual(self.probe.status()["counts"]["physics_time_not_advanced"], 1)

    def test_mapping_exception_does_not_fall_back_to_current_sim_time(self):
        self.emit()
        original = self.probe.latest("third")["metadata"]
        self.runtime.mapping_error = RuntimeError("mapping_unavailable")
        self.clock.advance(0.1)
        self.emit(numerator=2, sim_time=0.4)
        self.assertEqual(self.probe.latest("third")["metadata"], original)
        self.assertIn("mapping_unavailable", (self.output / "diagnostics.jsonl").read_text())

    def test_rgb_failure_does_not_consume_reference_or_create_new_frame(self):
        self.emit()
        self.rgb["third"].error = RuntimeError("rgb_unavailable")
        self.clock.advance(0.1)
        self.emit(numerator=2, sim_time=0.4)
        self.assertEqual(self.probe.latest("third")["metadata"]["completed_seq"], 1)
        self.rgb["third"].error = None
        self.runtime.emit(self.paths["third"])
        self.assertEqual(self.probe.latest("third")["metadata"]["completed_seq"], 2)

    def test_reference_changing_during_rgb_read_cannot_pair_frames(self):
        self.rgb["third"].after_read = lambda: self.runtime.sample(
            self.paths["third"], 2, 5, 0.4)
        self.emit()
        self.assertIsNone(self.probe.latest("third"))
        self.assertEqual(self.probe.status()["counts"]["reference_changed_during_rgb_read"], 1)

    def test_pause_during_rgb_read_refuses_frame(self):
        self.rgb["third"].after_read = lambda: setattr(self.runtime, "is_playing", False)
        self.emit()
        self.assertIsNone(self.probe.latest("third"))
        self.assertNotIn("completed", self.probe.status()["counts"])

    def test_frame_and_nested_metadata_are_detached_from_mutable_buffers(self):
        self.emit()
        first = self.probe.latest("third")
        self.rgb["third"].value[:] = 100
        first["rgb"][:] = 200
        first["metadata"]["reference_time"]["numerator"] = 999
        again = self.probe.latest("third")
        self.assertTrue(np.all(again["rgb"] == 23))
        self.assertEqual(again["metadata"]["reference_time"]["numerator"], 1)

    def test_stale_cache_or_runtime_exception_is_unavailable(self):
        self.emit()
        self.clock.advance(2)
        self.assertIsNone(self.probe.latest("third", max_age_seconds=1.5))
        self.runtime.playing_error = RuntimeError("timeline_offline")
        self.assertIsNone(self.probe.latest("third"))
        self.assertEqual(self.probe.status()["counts"]["latest_runtime_error"], 1)

    def test_invalid_reference_rgb_or_future_mapping_cannot_create_sample(self):
        self.emit(denominator=0)
        self.assertIsNone(self.probe.latest("third"))
        self.rgb["third"].value = np.zeros((0, 4, 3), dtype=np.uint8)
        self.emit()
        self.assertIsNone(self.probe.latest("third"))
        self.rgb["third"].value = np.ones((3, 4, 3), dtype=np.uint8)
        self.runtime.sample(self.paths["third"], 1, 5, 10)
        self.runtime.observed = 1.0
        self.runtime.emit(self.paths["third"])
        self.assertIsNone(self.probe.latest("third"))
        self.assertEqual(self.probe.status()["counts"]["mapped_sim_time_in_future"], 1)

    def test_existing_evidence_directory_cannot_be_overwritten(self):
        with self.assertRaises(FileExistsError):
            CameraFrameProbe({"third": (self.paths["third"], self.rgb["third"])},
                             output_dir=self.output, _runtime=Runtime())

    def test_initialization_error_preserves_diagnostic_and_detaches(self):
        class BadRuntime(Runtime):
            def attach_reference(self, path):
                super().attach_reference(path)
                raise RuntimeError("ReferenceTime_attach_unavailable")
        runtime = BadRuntime()
        output = Path(self.temporary.name) / "initialization-error"
        with self.assertRaisesRegex(RuntimeError, "ReferenceTime_attach_unavailable"):
            CameraFrameProbe({"third": (self.paths["third"], self.rgb["third"])},
                             output_dir=output, _runtime=runtime)
        self.assertTrue(runtime.closed)
        self.assertIn("initialization_error", (output / "diagnostics.jsonl").read_text())
        self.assertFalse((output / "frames.jsonl").exists())

    def test_path_is_explicit_and_repr_is_not_a_path_fallback(self):
        class ReprOnly:
            def __repr__(self):
                return "/Render/looks_like_a_path"
        with self.assertRaisesRegex(ValueError, "render_product_path_required"):
            CameraFrameProbe({"bad": (ReprOnly(), self.rgb["third"])},
                             output_dir=Path(self.temporary.name) / "invalid", _runtime=Runtime())


class CameraTickRateApiTests(unittest.TestCase):
    def sdk(self, prim_type="Camera"):
        class Attribute:
            def __init__(self):
                self.value = 60.0
            def Get(self):
                return self.value
        attribute = Attribute()
        prim = types.SimpleNamespace(IsValid=lambda: True,
                                     GetTypeName=lambda: prim_type,
                                     GetAttribute=lambda name: attribute)
        stage = types.SimpleNamespace(GetPrimAtPath=lambda path: prim)
        modules = {}
        for name in ("omni", "omni.usd", "isaacsim", "isaacsim.sensors",
                     "isaacsim.sensors.experimental", "isaacsim.sensors.experimental.rtx"):
            modules[name] = types.ModuleType(name)
            modules[name].__path__ = []
        modules["omni"].usd = modules["omni.usd"]
        modules["omni.usd"].get_context = lambda: types.SimpleNamespace(get_stage=lambda: stage)
        calls = []
        def camera(**kwargs):
            calls.append(kwargs)
            attribute.value = kwargs["tick_rate"]
        modules["isaacsim.sensors.experimental.rtx"].RtxCamera = camera
        return modules, calls

    def test_explicit_rate_api_uses_fixed_signature_without_resetting_transform(self):
        modules, calls = self.sdk()
        with patch.dict("sys.modules", modules):
            result = set_camera_tick_rate("/World/existing_camera", 5.0)
        self.assertEqual(calls, [{"path": "/World/existing_camera", "tick_rate": 5.0,
                                  "reset_xform_op_properties": False}])
        self.assertEqual(result["previous_tick_rate"], 60.0)
        self.assertEqual(result["actual_tick_rate"], 5.0)

    def test_lidar_cannot_be_modified_by_the_explicit_camera_api(self):
        modules, calls = self.sdk(prim_type="OmniLidar")
        with patch.dict("sys.modules", modules):
            with self.assertRaisesRegex(ValueError, "existing_camera_prim_required"):
                set_camera_tick_rate("/World/Lidar", 5.0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
