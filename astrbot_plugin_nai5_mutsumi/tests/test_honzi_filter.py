"""本地 NSFW 过滤：可 mock，且拒绝云端视觉。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from honzi_filter import (  # noqa: E402
    CloudVisionForbidden,
    LocalNsfwFilter,
    NsfwBox,
    apply_censor,
    assert_local_backend,
    decide_from_detections,
)


def _tiny_jpeg(path: Path, color=(200, 160, 140)) -> Path:
    from PIL import Image

    im = Image.new("RGB", (64, 64), color)
    im.save(path, format="JPEG", quality=85)
    return path


class TestBackendGuard(unittest.TestCase):
    def test_forbids_cloud_vision(self):
        for name in ("deepseek", "openai", "gpt-4v", "cloud", "https://x", "qwen-vl"):
            with self.assertRaises(CloudVisionForbidden):
                assert_local_backend(name)
            with self.assertRaises(CloudVisionForbidden):
                LocalNsfwFilter(backend=name)

    def test_local_backends_ok(self):
        for name in ("auto", "nudenet", "opencv", "heuristic"):
            self.assertEqual(assert_local_backend(name), name)


class TestDetectionsMock(unittest.TestCase):
    def test_drop_on_exposed_genitalia(self):
        d = decide_from_detections(
            [
                {
                    "class": "FEMALE_GENITALIA_EXPOSED",
                    "score": 0.91,
                    "box": [10, 20, 30, 40],
                }
            ]
        )
        self.assertEqual(d.action, "drop")
        self.assertFalse(d.cloud)
        self.assertIn("GENITALIA", d.reason)

    def test_censor_on_breast(self):
        d = decide_from_detections(
            [
                {
                    "class": "FEMALE_BREAST_EXPOSED",
                    "score": 0.77,
                    "box": [5, 5, 20, 20],
                }
            ]
        )
        self.assertEqual(d.action, "censor")
        self.assertEqual(len(d.boxes), 1)

    def test_keep_when_clean(self):
        d = decide_from_detections(
            [{"class": "FACE_FEMALE", "score": 0.99, "box": [1, 1, 8, 8]}]
        )
        self.assertEqual(d.action, "keep")

    def test_filter_with_mocked_nudenet(self):
        def fake_detect(_path: str):
            return [
                {
                    "class": "ANUS_EXPOSED",
                    "score": 0.8,
                    "box": [1, 1, 10, 10],
                }
            ]

        filt = LocalNsfwFilter(backend="nudenet", detect_fn=fake_detect)
        self.assertTrue(filt.is_local)
        self.assertNotIn("deepseek", filt.backend_id.lower())
        with tempfile.TemporaryDirectory() as td:
            src = _tiny_jpeg(Path(td) / "p.jpg")
            out = Path(td) / "kept"
            kept, results = filt.filter_pages([src], out)
            self.assertEqual(kept, [])
            self.assertEqual(results[0].decision.action, "drop")
            self.assertEqual(results[0].decision.backend, "nudenet-mock")

    def test_censor_paints_black(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "a.jpg"
            from PIL import Image

            Image.new("RGB", (40, 40), (255, 0, 0)).save(src)
            dest = Path(td) / "b.jpg"
            apply_censor(src, dest, [NsfwBox(0, 0, 40, 40, "X", 1.0)])
            out = Image.open(dest)
            px = out.getpixel((20, 20))
            out.close()
            self.assertEqual(px, (0, 0, 0))

    def test_heuristic_runs_without_cloud(self):
        filt = LocalNsfwFilter(backend="heuristic")
        self.assertEqual(filt.backend_id, "heuristic")
        with tempfile.TemporaryDirectory() as td:
            # 低皮肤蓝图应 keep
            src = _tiny_jpeg(Path(td) / "blue.jpg", color=(20, 40, 180))
            d = filt.decide(src)
            self.assertEqual(d.action, "keep")
            self.assertFalse(d.cloud)


if __name__ == "__main__":
    unittest.main()
