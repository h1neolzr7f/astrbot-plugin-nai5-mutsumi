"""本子页本地 NSFW 过滤：整页剔除或局部遮挡。

强制约束
========
- **禁止** DeepSeek / OpenAI / 任何云端视觉 API。
- 只在本机跑：NudeNet ONNX（可选）→ OpenCV 皮肤启发式 → Pillow 启发式。
- 权重路径可配置；缺权重自动降级，不发起 HTTP 下载审查模型。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

logger = logging.getLogger("astrbot_plugin_nai5_mutsumi.honzi_filter")

FilterAction = Literal["keep", "censor", "drop"]
BackendName = Literal["auto", "nudenet", "opencv", "heuristic"]

# 明确拒绝的云端/远程后端名（防误配）
_FORBIDDEN_BACKENDS = frozenset(
    {
        "deepseek",
        "openai",
        "gpt-4v",
        "gpt4v",
        "claude",
        "gemini",
        "qwen-vl",
        "qwen_vl",
        "cloud",
        "http",
        "https",
        "vision-api",
        "vision_api",
        "azure",
    }
)

# NudeNet / 兼容标签：外生殖器、肛门 → 整页剔除（QQ 不过审）
_DROP_CLASSES = frozenset(
    {
        "FEMALE_GENITALIA_EXPOSED",
        "MALE_GENITALIA_EXPOSED",
        "ANUS_EXPOSED",
        "EXPOSED_GENITALIA_F",
        "EXPOSED_GENITALIA_M",
        "EXPOSED_ANUS",
        "GENITALIA_EXPOSED",
    }
)
# 裸胸/臀 → 局部遮挡后保留
_CENSOR_CLASSES = frozenset(
    {
        "FEMALE_BREAST_EXPOSED",
        "MALE_BREAST_EXPOSED",
        "BUTTOCKS_EXPOSED",
        "EXPOSED_BREAST_F",
        "EXPOSED_BREAST_M",
        "EXPOSED_BUTTOCKS",
        "BELLY_EXPOSED",
    }
)


@dataclass
class NsfwBox:
    x: int
    y: int
    w: int
    h: int
    label: str = ""
    score: float = 0.0


@dataclass
class FilterDecision:
    action: FilterAction
    score: float
    reason: str
    boxes: list[NsfwBox] = field(default_factory=list)
    backend: str = "heuristic"

    @property
    def cloud(self) -> bool:
        return False


@dataclass
class FilterPageResult:
    src: Path
    dest: Path | None
    decision: FilterDecision


class CloudVisionForbidden(ValueError):
    """配置了云端视觉后端时抛出，保证审查永不离机。"""


def assert_local_backend(name: str) -> str:
    raw = (name or "auto").strip().lower() or "auto"
    if raw in _FORBIDDEN_BACKENDS or raw.startswith(("http://", "https://")):
        raise CloudVisionForbidden(
            f"禁止云端视觉 NSFW 审查：backend={name!r}。"
            "请用 auto / nudenet / opencv / heuristic。"
        )
    if raw not in {"auto", "nudenet", "opencv", "heuristic"}:
        raise CloudVisionForbidden(
            f"未知 NSFW 后端 {name!r}（只允许本地 auto/nudenet/opencv/heuristic）"
        )
    return raw


def _norm_label(label: str) -> str:
    return (label or "").strip().upper().replace(" ", "_")


def _box_from_det(det: dict[str, Any]) -> NsfwBox | None:
    label = _norm_label(str(det.get("class") or det.get("label") or det.get("name") or ""))
    try:
        score = float(det.get("score") or det.get("confidence") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    box = det.get("box") or det.get("bbox") or det.get("rectangle")
    if not box or len(box) < 4:
        return None
    x1, y1, a, b = [int(float(v)) for v in box[:4]]
    if det.get("xyxy"):
        w, h = a - x1, b - y1
    else:
        # NudeNet 官方 detector 输出 [x, y, w, h]
        w, h = a, b
    return NsfwBox(x=max(0, x1), y=max(0, y1), w=max(1, w), h=max(1, h), label=label, score=score)


def decide_from_detections(
    detections: Iterable[dict[str, Any]],
    *,
    drop_threshold: float = 0.6,
    censor_threshold: float = 0.5,
    backend: str = "nudenet",
) -> FilterDecision:
    boxes: list[NsfwBox] = []
    drop_score = 0.0
    censor_score = 0.0
    drop_hits: list[str] = []
    censor_hits: list[str] = []
    for det in detections or []:
        if not isinstance(det, dict):
            continue
        box = _box_from_det(det)
        if box is None:
            continue
        boxes.append(box)
        lab = box.label
        if lab in _DROP_CLASSES and box.score >= drop_threshold:
            drop_score = max(drop_score, box.score)
            drop_hits.append(lab)
        elif lab in _CENSOR_CLASSES and box.score >= censor_threshold:
            censor_score = max(censor_score, box.score)
            censor_hits.append(lab)
        # 有的 NudeNet 把 COVERED_* 也吐出，忽略
    if drop_hits:
        return FilterDecision(
            action="drop",
            score=drop_score,
            reason="exposed_genitalia:" + ",".join(sorted(set(drop_hits))),
            boxes=boxes,
            backend=backend,
        )
    if censor_hits:
        return FilterDecision(
            action="censor",
            score=censor_score,
            reason="exposed_partial:" + ",".join(sorted(set(censor_hits))),
            boxes=[b for b in boxes if b.label in _CENSOR_CLASSES],
            backend=backend,
        )
    return FilterDecision(
        action="keep",
        score=max((b.score for b in boxes), default=0.0),
        reason="clean",
        boxes=[],
        backend=backend,
    )


def _open_rgb(path: Path):
    from PIL import Image

    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    else:
        im = im.copy()
    return im


def apply_censor(src: Path, dest: Path, boxes: list[NsfwBox]) -> Path:
    """局部涂黑。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    im = _open_rgb(src)
    from PIL import ImageDraw

    draw = ImageDraw.Draw(im)
    w, h = im.size
    for b in boxes:
        x1 = max(0, b.x)
        y1 = max(0, b.y)
        x2 = min(w, b.x + max(1, b.w))
        y2 = min(h, b.y + max(1, b.h))
        if x2 <= x1 or y2 <= y1:
            continue
        # 略放大，避免边露
        pad_x = max(4, int((x2 - x1) * 0.08))
        pad_y = max(4, int((y2 - y1) * 0.08))
        draw.rectangle(
            [max(0, x1 - pad_x), max(0, y1 - pad_y), min(w, x2 + pad_x), min(h, y2 + pad_y)],
            fill=(0, 0, 0),
        )
    im.save(dest, format="JPEG", quality=90)
    im.close()
    return dest


def _skin_mask_pil(im) -> list[list[int]]:
    """粗皮肤掩码：YCbCr + RGB 启发式，不依赖 OpenCV。"""
    w, h = im.size
    px = im.load()
    mask: list[list[int]] = []
    ycb = im.convert("YCbCr")
    ypx = ycb.load()
    for y in range(h):
        row: list[int] = []
        for x in range(w):
            r, g, b = px[x, y][:3]
            yy, cb, cr = ypx[x, y][:3]
            skin = (
                77 <= cb <= 127
                and 133 <= cr <= 173
                and r > 80
                and r >= g
                and r >= b
            )
            row.append(1 if skin else 0)
        mask.append(row)
    ycb.close()
    return mask


def _region_ratio(mask: list[list[int]], x0: float, y0: float, x1: float, y1: float) -> float:
    h = len(mask)
    w = len(mask[0]) if h else 0
    if w == 0 or h == 0:
        return 0.0
    xa, xb = int(x0 * w), int(x1 * w)
    ya, yb = int(y0 * h), int(y1 * h)
    xa, xb = max(0, min(w, xa)), max(0, min(w, xb))
    ya, yb = max(0, min(h, ya)), max(0, min(h, yb))
    if xb <= xa or yb <= ya:
        return 0.0
    total = 0
    skin = 0
    step = max(1, (xb - xa) // 80, (yb - ya) // 80)
    for y in range(ya, yb, step):
        row = mask[y]
        for x in range(xa, xb, step):
            total += 1
            skin += row[x]
    return (skin / total) if total else 0.0


def decide_from_skin_heuristic(
    path: Path,
    *,
    backend: str = "heuristic",
    drop_groin: float = 0.32,
    censor_chest: float = 0.38,
    min_skin: float = 0.12,
) -> FilterDecision:
    """封面/对白页皮肤少 → keep；下腹高皮肤 → drop；胸部高皮肤 → censor。"""
    im = _open_rgb(path)
    try:
        w, h = im.size
        mask = _skin_mask_pil(im)
        overall = _region_ratio(mask, 0, 0, 1, 1)
        groin = _region_ratio(mask, 0.28, 0.52, 0.72, 0.88)
        chest = _region_ratio(mask, 0.22, 0.22, 0.78, 0.52)
        if overall < min_skin:
            return FilterDecision(
                action="keep",
                score=overall,
                reason=f"low_skin:{overall:.2f}",
                backend=backend,
            )
        if groin >= drop_groin and overall >= 0.18:
            return FilterDecision(
                action="drop",
                score=groin,
                reason=f"heuristic_groin:{groin:.2f}",
                backend=backend,
            )
        if chest >= censor_chest:
            boxes = [
                NsfwBox(
                    x=int(0.18 * w),
                    y=int(0.20 * h),
                    w=int(0.64 * w),
                    h=int(0.34 * h),
                    label="CHEST_SKIN",
                    score=chest,
                )
            ]
            return FilterDecision(
                action="censor",
                score=chest,
                reason=f"heuristic_chest:{chest:.2f}",
                boxes=boxes,
                backend=backend,
            )
        return FilterDecision(
            action="keep",
            score=overall,
            reason=f"skin_ok:{overall:.2f}",
            backend=backend,
        )
    finally:
        im.close()


def decide_from_opencv(path: Path, **kwargs: Any) -> FilterDecision:
    """OpenCV HSV 皮肤；失败则降级 Pillow 启发式。"""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return decide_from_skin_heuristic(path, backend="heuristic")

    img = cv2.imread(str(path))
    if img is None:
        return decide_from_skin_heuristic(path, backend="heuristic")
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    lower1 = (0, 30, 50)
    upper1 = (25, 180, 255)
    lower2 = (160, 30, 50)
    upper2 = (180, 180, 255)
    m1 = cv2.inRange(hsv, lower1, upper1)
    m2 = cv2.inRange(hsv, lower2, upper2)
    mask = cv2.bitwise_or(m1, m2)
    # 形态学去噪
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    h, w = mask.shape[:2]
    def ratio(x0, y0, x1, y1) -> float:
        xa, xb = int(x0 * w), int(x1 * w)
        ya, yb = int(y0 * h), int(y1 * h)
        roi = mask[ya:yb, xa:xb]
        if roi.size == 0:
            return 0.0
        return float(np.count_nonzero(roi)) / float(roi.size)

    overall = ratio(0, 0, 1, 1)
    groin = ratio(0.28, 0.52, 0.72, 0.88)
    chest = ratio(0.22, 0.22, 0.78, 0.52)
    backend = "opencv"
    if overall < 0.12:
        return FilterDecision(action="keep", score=overall, reason=f"low_skin:{overall:.2f}", backend=backend)
    if groin >= 0.30 and overall >= 0.16:
        return FilterDecision(action="drop", score=groin, reason=f"opencv_groin:{groin:.2f}", backend=backend)
    if chest >= 0.36:
        return FilterDecision(
            action="censor",
            score=chest,
            reason=f"opencv_chest:{chest:.2f}",
            boxes=[NsfwBox(int(0.18 * w), int(0.20 * h), int(0.64 * w), int(0.34 * h), "CHEST_SKIN", chest)],
            backend=backend,
        )
    return FilterDecision(action="keep", score=overall, reason=f"skin_ok:{overall:.2f}", backend=backend)


def default_nudenet_paths(explicit: str = "") -> list[Path]:
    env = __import__("os").environ.get("NUDENET_MODEL_PATH") or ""
    home = Path.home()
    cands = []
    if explicit:
        cands.append(Path(explicit))
    if env:
        cands.append(Path(env))
    cands.extend(
        [
            home / ".NudeNet" / "detector_v2_default_checkpoint.onnx",
            home / ".NudeNet" / "320n.onnx",
            Path("/AstrBot/data/plugin_data/astrbot_plugin_nai5_mutsumi/nudenet/detector.onnx"),
            Path(__file__).resolve().parent / "nudenet" / "detector.onnx",
        ]
    )
    return cands


class LocalNsfwFilter:
    """可注入 detector 的本地过滤器。测试可 mock ``detect_fn``。"""

    def __init__(
        self,
        backend: str = "auto",
        model_path: str = "",
        drop_threshold: float = 0.6,
        censor_threshold: float = 0.5,
        detect_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.wanted = assert_local_backend(backend)
        self.model_path = (model_path or "").strip()
        self.drop_threshold = float(drop_threshold)
        self.censor_threshold = float(censor_threshold)
        self._detect_fn = detect_fn
        self._resolved = self._resolve_backend()

    @property
    def backend_id(self) -> str:
        return self._resolved

    @property
    def is_local(self) -> bool:
        return True

    def _resolve_backend(self) -> str:
        if self._detect_fn is not None:
            return "nudenet-mock" if self.wanted in {"auto", "nudenet"} else self.wanted
        if self.wanted == "heuristic":
            return "heuristic"
        if self.wanted == "opencv":
            return "opencv"
        if self.wanted in {"auto", "nudenet"}:
            if self._nudenet_ready():
                return "nudenet"
            if self.wanted == "nudenet":
                logger.warning(
                    "[honzi] NudeNet 不可用（未安装或无本地权重），降级 opencv/heuristic；"
                    "不会下载云端模型，更不会走 DeepSeek"
                )
            try:
                import cv2  # noqa: F401

                return "opencv"
            except ImportError:
                return "heuristic"
        return "heuristic"

    def _nudenet_ready(self) -> bool:
        try:
            import nudenet  # noqa: F401
        except ImportError:
            return False
        for p in default_nudenet_paths(self.model_path):
            if p.is_file():
                if not self.model_path:
                    self.model_path = str(p)
                return True
        # 没有本地 onnx 就不启用，避免 NudeNet 首次 init 去拉权重
        return False

    def _nudenet_detect(self, image_path: str) -> list[dict[str, Any]]:
        if self._detect_fn is not None:
            return list(self._detect_fn(image_path) or [])
        if not self.model_path or not Path(self.model_path).is_file():
            raise RuntimeError("NudeNet 无本地权重，拒绝初始化（防联网下载）")
        from nudenet import NudeDetector  # type: ignore

        detector = NudeDetector(model_path=self.model_path)
        raw = detector.detect(image_path)
        return list(raw or [])

    def decide(self, image_path: str | Path) -> FilterDecision:
        path = Path(image_path)
        logger.info(
            "[honzi] nsfw_filter decide backend=%s cloud=never path=%s",
            self._resolved,
            path.name,
        )
        if self._resolved.startswith("nudenet"):
            try:
                dets = self._nudenet_detect(str(path))
                return decide_from_detections(
                    dets,
                    drop_threshold=self.drop_threshold,
                    censor_threshold=self.censor_threshold,
                    backend=self._resolved,
                )
            except CloudVisionForbidden:
                raise
            except Exception as e:
                logger.warning("[honzi] NudeNet 失败，降级启发式: %s", e)
                return decide_from_skin_heuristic(path, backend="heuristic")
        if self._resolved == "opencv":
            return decide_from_opencv(path)
        return decide_from_skin_heuristic(path, backend="heuristic")

    def apply_one(self, src: Path, dest_dir: Path) -> FilterPageResult:
        decision = self.decide(src)
        if decision.action == "drop":
            return FilterPageResult(src=src, dest=None, decision=decision)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if dest.suffix.lower() not in {".jpg", ".jpeg"}:
            dest = dest.with_suffix(".jpg")
        if decision.action == "censor" and decision.boxes:
            apply_censor(src, dest, decision.boxes)
            return FilterPageResult(src=src, dest=dest, decision=decision)
        # keep：拷贝为 JPEG 统一后续反推
        im = _open_rgb(src)
        try:
            im.save(dest, format="JPEG", quality=92)
        finally:
            im.close()
        return FilterPageResult(src=src, dest=dest, decision=decision)

    def filter_pages(
        self, pages: Iterable[Path], dest_dir: Path
    ) -> tuple[list[Path], list[FilterPageResult]]:
        kept: list[Path] = []
        results: list[FilterPageResult] = []
        for src in pages:
            r = self.apply_one(Path(src), dest_dir)
            results.append(r)
            logger.info(
                "[honzi] nsfw_filter page=%s action=%s score=%.3f reason=%s backend=%s cloud=never",
                Path(src).name,
                r.decision.action,
                r.decision.score,
                r.decision.reason,
                r.decision.backend,
            )
            if r.dest is not None:
                kept.append(r.dest)
        return kept, results
