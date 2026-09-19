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

class TestHeuristicOpenCVPolicy(unittest.TestCase):
    """人造图：蓝图 keep；高皮肤下腹 drop；胸部区 censor；禁止 cloud。"""

    def _make(self, path: Path, paint):
        from PIL import Image, ImageDraw

        im = Image.new("RGB", (200, 300), (20, 40, 180))
        draw = ImageDraw.Draw(im)
        paint(draw, im)
        im.save(path, format="JPEG", quality=90)
        return path

    def test_blue_keep(self):
        filt = LocalNsfwFilter(backend="heuristic")
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "blue.jpg"
            self._make(src, lambda d, im: None)
            d = filt.decide(src)
            self.assertEqual(d.action, "keep")
            self.assertFalse(d.cloud)

    def test_artificial_groin_drop(self):
        filt = LocalNsfwFilter(backend="heuristic")

        def paint(draw, im):
            # 下腹 ROI 填肤色（高面积）
            w, h = im.size
            draw.rectangle(
                [int(0.35 * w), int(0.58 * h), int(0.65 * w), int(0.82 * h)],
                fill=(210, 160, 130),
            )
            # 整页也铺一些肤色抬 overall
            draw.rectangle([0, int(0.2 * h), w, h], fill=(205, 155, 125))

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "groin.jpg"
            self._make(src, paint)
            d = filt.decide(src)
            self.assertEqual(d.action, "drop", d.reason)
            self.assertFalse(d.cloud)

    def test_artificial_chest_censor(self):
        filt = LocalNsfwFilter(backend="heuristic")

        def paint(draw, im):
            w, h = im.size
            # 仅胸部高肤，下腹保持蓝
            draw.rectangle(
                [int(0.22 * w), int(0.22 * h), int(0.78 * w), int(0.52 * h)],
                fill=(215, 165, 135),
            )

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "chest.jpg"
            self._make(src, paint)
            d = filt.decide(src)
            self.assertEqual(d.action, "censor", d.reason)
            self.assertTrue(d.boxes)
            self.assertFalse(d.cloud)

    def test_soft_groin_becomes_censor_not_drop(self):
        """中等下腹肤色不应硬 drop，应 censor。"""
        from honzi_filter import decide_from_skin_heuristic

        def paint(draw, im):
            w, h = im.size
            # 中等皮肤：够 soft 不够 hard
            draw.rectangle(
                [int(0.35 * w), int(0.58 * h), int(0.65 * w), int(0.82 * h)],
                fill=(210, 160, 130),
            )
            # overall 适中：只涂下半一点
            draw.rectangle(
                [int(0.2 * w), int(0.5 * h), int(0.8 * w), int(0.9 * h)],
                fill=(200, 150, 120),
            )

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "soft.jpg"
            self._make(src, paint)
            d = decide_from_skin_heuristic(src, drop_groin=0.70, soft_groin=0.28)
            self.assertIn(d.action, ("censor", "keep"), d.reason)
            self.assertNotEqual(d.action, "drop")

    def test_opencv_fallback_label(self):
        filt = LocalNsfwFilter(backend="auto")
        # 无 nudenet 权重时多为 opencv/heuristic
        if filt.backend_id in {"opencv", "heuristic"}:
            self.assertTrue(filt.is_opencv_fallback)
            self.assertIn("降级", filt.user_backend_label())
        self.assertTrue(filt.is_local)

    def test_cloud_still_forbidden(self):
        with self.assertRaises(CloudVisionForbidden):
            LocalNsfwFilter(backend="deepseek")

