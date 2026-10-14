import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from mhs.camera_jpeg_sink import CameraJpegSink


class Clock:
    def __init__(self):
        self.wall = 1000.
        self.monotonic = 50.

    def advance(self, seconds):
        self.wall += seconds
        self.monotonic += seconds


class CameraJpegSinkTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[1]
        cache = workspace / ".cache/camera-jpeg-tests"
        cache.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="logic-", dir=cache)
        self.root = Path(self.temporary.name)
        if not self.root.resolve().is_relative_to(workspace):
            raise RuntimeError("test_directory_outside_workspace")
        self.clock = Clock()
        self.sink = CameraJpegSink(self.root, _wall_clock=lambda: self.clock.wall,
                                   _monotonic_clock=lambda: self.clock.monotonic)

    def tearDown(self):
        self.temporary.cleanup()

    def frame(self, view="third_person", seq=1, sim_time=1., age=0.):
        return {"rgb": np.full((4, 8, 4), 110, dtype=np.uint8), "metadata": {
            "schema_version": 1, "source": view, "render_product_path": "/Render/" + view,
            "completed_seq": seq, "reference_time": {"numerator": int(sim_time * 60), "denominator": 60},
            "raw_reference_time": {"referenceTimeNumerator": int(sim_time * 60), "referenceTimeDenominator": 60},
            "callback_wall_time": self.clock.wall - age,
            "callback_monotonic": self.clock.monotonic - age,
            "rgb_copied_wall_time": self.clock.wall - age,
            "mapped_sim_time": sim_time, "observed_sim_time": sim_time,
            "simulation_advancing": True, "rgb_shape": [4, 8, 4], "rgb_dtype": "uint8",
        }}

    def publish(self, frame=None, view="third_person", **kwargs):
        return self.sink.publish(view, frame, camera={"mount": {"height": .6}},
                                 simulation_playing=kwargs.get("playing", True))

    def endpoints(self, view="third_person"):
        image, meta = (("latest.jpg", "latest-frame.json") if view == "third_person"
                       else ("first-person.jpg", "first-person-frame.json"))
        return (self.root / "runs" / image, self.root / "runs" / meta)

    def assertUnchanged(self, before, view="third_person"):
        self.assertEqual(before, tuple(path.read_bytes() for path in self.endpoints(view)))

    def test_original_time_reference_and_pixel_frame_survive_publication(self):
        frame = self.frame(age=.1)
        original = copy.deepcopy(frame["metadata"])
        result = self.publish(frame)
        self.assertTrue(result["written"])
        image, metadata = self.endpoints()
        meta = json.loads(metadata.read_text())
        self.assertEqual(meta["capture_wall_time"], original["callback_wall_time"])
        self.assertEqual(meta["sim_time"], original["mapped_sim_time"])
        self.assertEqual(meta["frame_clock"], original)
        self.assertEqual(meta["reference_time"], original["reference_time"])
        self.assertEqual(meta["completed_seq"], 1)
        self.assertEqual(meta["encoded_wall_time"], self.clock.wall)
        self.assertEqual(meta["sha256"], hashlib.sha256(image.read_bytes()).hexdigest())
        self.assertEqual((meta["width"], meta["height"]), (8, 4))
        self.assertEqual(result["image"].size, (8, 4))
        frame["rgb"][:] = 0
        self.assertEqual(result["image"].getpixel((0, 0)), (110, 110, 110))
        frame["metadata"]["reference_time"]["numerator"] = 0
        self.assertEqual(result["metadata"]["reference_time"], original["reference_time"])

    def test_no_frame_pause_and_unconfirmed_timeline_never_rewrite(self):
        self.publish(self.frame())
        before = tuple(path.read_bytes() for path in self.endpoints())
        self.clock.advance(1)
        for frame, playing, reason in [(None, True, "no_completed_frame"),
                (self.frame(seq=2, sim_time=2), False, "simulation_not_playing"),
                (self.frame(seq=2, sim_time=2), None, "simulation_not_playing")]:
            result = self.publish(frame, playing=playing)
            self.assertFalse(result["written"])
            self.assertEqual(result["reason"], reason)
            self.assertUnchanged(before)

    def test_old_or_repeated_sequence_cannot_refresh_a_frame(self):
        self.publish(self.frame(seq=3, sim_time=3))
        before = tuple(path.read_bytes() for path in self.endpoints())
        self.clock.advance(1)
        for seq in (3, 2):
            result = self.publish(self.frame(seq=seq, sim_time=4))
            self.assertFalse(result["written"])
            self.assertIn(result["reason"], ("completed_seq_not_advanced", "completed_seq_out_of_order",
                                           "completed_seq_reused_with_changed_frame"))
            self.assertUnchanged(before)

    def test_new_sequence_requires_advanced_reference_and_sim_time(self):
        self.publish(self.frame())
        self.clock.advance(1)
        frame = self.frame(seq=2, sim_time=2)
        frame["metadata"]["reference_time"] = {"numerator": 120, "denominator": 120}
        self.assertEqual(self.publish(frame)["reason"], "reference_time_not_advanced")
        frame = self.frame(seq=2, sim_time=2)
        frame["metadata"]["mapped_sim_time"] = 1.
        self.assertEqual(self.publish(frame)["reason"], "mapped_sim_time_not_advanced")

    def test_rate_limit_is_per_view_and_does_not_consume_skipped_sequence(self):
        self.assertTrue(self.publish(self.frame())["written"])
        self.assertTrue(self.publish(self.frame("first_person"), "first_person")["written"])
        self.clock.advance(.99)
        completion = self.frame(seq=2, sim_time=2)
        self.assertEqual(self.publish(completion)["reason"], "wall_rate_limited")
        self.clock.advance(.01)
        self.assertTrue(self.publish(completion)["written"])
        first = self.publish(self.frame("first_person", seq=2, sim_time=2), "first_person")
        self.assertTrue(first["written"])
        self.assertNotIn("image", first)

    def test_rate_skipped_newer_completion_still_rejects_an_older_queued_frame(self):
        self.publish(self.frame())
        self.clock.advance(.9)
        newest = self.frame(seq=3, sim_time=3)
        self.assertEqual(self.publish(newest)["reason"], "wall_rate_limited")
        self.clock.advance(.1)
        older = self.frame(seq=2, sim_time=2, age=.1)
        self.assertEqual(self.publish(older)["reason"], "completed_seq_out_of_order")
        self.assertTrue(self.publish(newest)["written"])

    def test_same_completion_cannot_be_retimestamped_after_rate_skip(self):
        self.publish(self.frame())
        self.clock.advance(.9)
        newest = self.frame(seq=2, sim_time=2)
        self.assertEqual(self.publish(newest)["reason"], "wall_rate_limited")
        self.clock.advance(.1)
        self.assertEqual(self.publish(self.frame(seq=2, sim_time=2))["reason"],
                         "completed_seq_reused_with_changed_frame")
        self.assertTrue(self.publish(newest)["written"])

    def test_stale_or_future_callback_refused_without_any_output(self):
        for age in (.350001, -.01):
            result = self.publish(self.frame(age=age))
            self.assertEqual(result["reason"], "callback_stale_or_future")
        frame = self.frame()
        frame["metadata"]["callback_monotonic"] -= .36
        self.assertEqual(self.publish(frame)["reason"], "callback_stale_or_future")
        self.assertFalse((self.root / "runs").exists())

    def test_encoding_delay_cannot_stamp_or_publish_expired_frame(self):
        from PIL import Image
        original = Image.Image.save

        def delayed(image, *args, **kwargs):
            original(image, *args, **kwargs)
            self.clock.advance(.36)

        with patch.object(Image.Image, "save", delayed):
            result = self.publish(self.frame())
        self.assertEqual(result["reason"], "callback_stale_or_future")
        self.assertFalse((self.root / "runs").exists())

    def test_staging_delay_cannot_replace_existing_endpoints(self):
        self.publish(self.frame())
        before = tuple(path.read_bytes() for path in self.endpoints())
        self.clock.advance(1)
        original = Path.write_text

        def delayed(path, *args, **kwargs):
            written = original(path, *args, **kwargs)
            if ".sink-" in path.name:
                self.clock.advance(.36)
            return written

        with patch.object(Path, "write_text", delayed):
            result = self.publish(self.frame(seq=2, sim_time=2))
        self.assertEqual(result["reason"], "callback_stale_or_future")
        self.assertUnchanged(before)
        self.assertEqual(len(list((self.root / "runs").iterdir())), 2)

    def test_wrong_view_product_or_progress_are_refused(self):
        self.assertEqual(self.publish(self.frame("first_person"))["reason"], "completed_frame_source_or_progress_invalid")
        frame = self.frame()
        frame["metadata"]["simulation_advancing"] = False
        self.assertEqual(self.publish(frame)["reason"], "completed_frame_source_or_progress_invalid")
        self.publish(self.frame())
        self.clock.advance(1)
        frame = self.frame(seq=2, sim_time=2)
        frame["metadata"]["render_product_path"] = "/Render/other"
        self.assertEqual(self.publish(frame)["reason"], "render_product_changed")

    def test_invalid_rgb_reference_and_nonfinite_metadata_are_refused(self):
        frame = self.frame()
        frame["rgb"] = frame["rgb"].astype(float)
        self.assertEqual(self.publish(frame)["reason"], "completed_rgb_invalid")
        frame = self.frame()
        frame["metadata"]["reference_time"]["denominator"] = 0
        self.assertEqual(self.publish(frame)["reason"], "reference_time_invalid")
        frame = self.frame()
        frame["metadata"]["mapped_sim_time"] = float("nan")
        self.assertEqual(self.publish(frame)["reason"], "mapped_sim_time_invalid")
        self.assertFalse((self.root / "runs").exists())

    def test_write_error_does_not_consume_completion(self):
        frame = self.frame()
        with patch.object(Path, "write_bytes", side_effect=OSError("full disk")):
            result = self.publish(frame)
        self.assertEqual(result["reason"], "publication_error")
        self.assertIn("full disk", result["error"])
        self.assertTrue(self.publish(frame)["written"])

    def test_unknown_view_is_explicit_programmer_error(self):
        with self.assertRaisesRegex(ValueError, "unknown_camera_view"):
            self.publish(self.frame(), "lidar")


if __name__ == "__main__":
    unittest.main()
