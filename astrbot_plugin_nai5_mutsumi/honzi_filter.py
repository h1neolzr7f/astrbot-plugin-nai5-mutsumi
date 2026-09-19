"""本子页本地 NSFW 过滤：整页剔除或局部遮挡。

强制约束
========
- **禁止** DeepSeek / OpenAI / 任何云端视觉 API。
- 只在本机跑：理塘 censor.onnx（可选）→ NudeNet ONNX（可选）→ OpenCV → Pillow 启发式。
- 权重路径可配置；缺权重自动降级，不发起 HTTP 下载审查模型。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

logger = logging.getLogger("astrbot_plugin_nai5_mutsumi.honzi_filter")

FilterAction = Literal["keep", "censor", "drop"]
BackendName = Literal["auto", "litang", "baibaoxiang", "nudenet", "opencv", "heuristic"]

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
        # 理塘 YOLO 原始类名
        "PENIS",
        "PUSSY",
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
        # 理塘 YOLO 原始类名
        "NIPPLE_F",
        "NIPPLE",
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


_LOCAL_BACKENDS = frozenset(
    {"auto", "litang", "baibaoxiang", "nudenet", "opencv", "heuristic"}
)


def assert_local_backend(name: str) -> str:
    raw = (name or "auto").strip().lower() or "auto"
    if raw in _FORBIDDEN_BACKENDS or raw.startswith(("http://", "https://")):
        raise CloudVisionForbidden(
            f"禁止云端视觉 NSFW 审查：backend={name!r}。"
            "请用 auto / litang / nudenet / opencv / heuristic。"
        )
    if raw not in _LOCAL_BACKENDS:
        raise CloudVisionForbidden(
            f"未知 NSFW 后端 {name!r}（只允许本地 auto/litang/baibaoxiang/nudenet/opencv/heuristic）"
        )
    return "litang" if raw == "baibaoxiang" else raw


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


def _chest_censor_boxes(w: int, h: int, chest: float, groin: float = 0.0) -> list[NsfwBox]:
    """胸部黑块；可疑下腹一并遮，避免整页 drop。"""
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
    if groin >= 0.22:
        boxes.append(
            NsfwBox(
                x=int(0.32 * w),
                y=int(0.55 * h),
                w=int(0.36 * w),
                h=int(0.28 * h),
                label="GROIN_SKIN",
                score=groin,
            )
        )
    return boxes


def decide_from_skin_heuristic(
    path: Path,
    *,
    backend: str = "heuristic",
    drop_groin: float = 0.70,
    soft_groin: float = 0.28,
    censor_chest: float = 0.30,
    min_skin: float = 0.10,
    overall_drop: float = 0.35,
) -> FilterDecision:
    """封面/对白页皮肤少 → keep；硬下腹 → drop；可疑/裸胸 → censor（优先于整页扔）。

    缺 NudeNet 时 OpenCV/启发式偏严会误伤故事页；故提高硬 drop 门槛，
    并把「可疑但不稳」降为局部涂黑保留。
    """
    im = _open_rgb(path)
    try:
        w, h = im.size
        mask = _skin_mask_pil(im)
        overall = _region_ratio(mask, 0, 0, 1, 1)
        # 收窄下腹 ROI，减少大腿/衣物误检
        groin = _region_ratio(mask, 0.35, 0.58, 0.65, 0.82)
        chest = _region_ratio(mask, 0.22, 0.22, 0.78, 0.52)
        if overall < min_skin:
            return FilterDecision(
                action="keep",
                score=overall,
                reason=f"low_skin:{overall:.2f}",
                backend=backend,
            )
        # 硬 drop：下腹皮肤极高 + 整页也不低（二次确认：面积本身）
        if groin >= drop_groin and overall >= overall_drop:
            return FilterDecision(
                action="drop",
                score=groin,
                reason=f"heuristic_groin:{groin:.2f}",
                backend=backend,
            )
        # 裸胸或可疑下腹 → censor，不整页扔
        if chest >= censor_chest or (groin >= soft_groin and overall >= 0.14):
            score = max(chest, groin)
            reason = (
                f"heuristic_chest:{chest:.2f}"
                if chest >= censor_chest and chest >= groin
                else f"heuristic_soft_groin:{groin:.2f}"
            )
            return FilterDecision(
                action="censor",
                score=score,
                reason=reason,
                boxes=_chest_censor_boxes(w, h, max(chest, censor_chest), groin),
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
    """OpenCV HSV + YCbCr 二次确认；失败则降级 Pillow 启发式。

    硬 drop 门槛显著高于旧版（0.30），并要求连通域与 YCbCr 交叉确认，
    否则降为 censor，避免成人漫画故事/半露页几乎整本被扔。
    """
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
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    # YCbCr 交叉：抑制头发/背景暖色误检
    ycrcb = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    ymask = cv2.inRange(ycrcb, (0, 133, 77), (255, 173, 127))
    mask_y = cv2.bitwise_and(mask, ymask)
    h, w = mask.shape[:2]

    def ratio(m, x0, y0, x1, y1) -> float:
        xa, xb = int(x0 * w), int(x1 * w)
        ya, yb = int(y0 * h), int(y1 * h)
        roi = m[ya:yb, xa:xb]
        if roi.size == 0:
            return 0.0
        return float(np.count_nonzero(roi)) / float(roi.size)

    overall = ratio(mask, 0, 0, 1, 1)
    # 收窄下腹 ROI
    groin = ratio(mask, 0.35, 0.58, 0.65, 0.82)
    groin_y = ratio(mask_y, 0.35, 0.58, 0.65, 0.82)
    chest = ratio(mask, 0.22, 0.22, 0.78, 0.52)
    # 连通域：要求下腹存在较大连通皮肤块，避免散点误 drop
    xa, xb = int(0.35 * w), int(0.65 * w)
    ya, yb = int(0.58 * h), int(0.82 * h)
    roi = mask[ya:yb, xa:xb]
    max_cc = 0.0
    if roi.size:
        nlab, _labs, stats, _ = cv2.connectedComponentsWithStats(
            (roi > 0).astype("uint8"), 8
        )
        if nlab > 1:
            areas = [int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, nlab)]
            max_cc = max(areas) / float(roi.size)

    backend = "opencv"
    drop_groin = float(kwargs.get("drop_groin", 0.70))
    soft_groin = float(kwargs.get("soft_groin", 0.28))
    censor_chest = float(kwargs.get("censor_chest", 0.30))
    overall_drop = float(kwargs.get("overall_drop", 0.35))
    mcc_min = float(kwargs.get("mcc_min", 0.40))
    gy_min = float(kwargs.get("gy_min", 0.60))

    if overall < 0.10:
        return FilterDecision(
            action="keep", score=overall, reason=f"low_skin:{overall:.2f}", backend=backend
        )

    hard = (
        groin >= drop_groin
        and overall >= overall_drop
        and max_cc >= mcc_min
        and groin_y >= gy_min
    )
    if hard:
        return FilterDecision(
            action="drop",
            score=groin,
            reason=f"opencv_groin:{groin:.2f}/y:{groin_y:.2f}/cc:{max_cc:.2f}",
            backend=backend,
        )

    if chest >= censor_chest or (groin >= soft_groin and overall >= 0.14):
        score = max(chest, groin)
        reason = (
            f"opencv_chest:{chest:.2f}"
            if chest >= censor_chest and chest >= groin
            else f"opencv_soft_groin:{groin:.2f}"
        )
        return FilterDecision(
            action="censor",
            score=score,
            reason=reason,
            boxes=_chest_censor_boxes(w, h, max(chest, censor_chest), groin),
            backend=backend,
        )
    return FilterDecision(
        action="keep", score=overall, reason=f"skin_ok:{overall:.2f}", backend=backend
    )


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
        litang_model_path: str = "",
        drop_threshold: float = 0.6,
        censor_threshold: float = 0.5,
        detect_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.wanted = assert_local_backend(backend)
        self.model_path = (model_path or "").strip()
        self.drop_threshold = float(drop_threshold)
        self.censor_threshold = float(censor_threshold)
        self._detect_fn = detect_fn
        self._litang_model_path = (litang_model_path or "").strip()
        self._detector_wanted = self.wanted in {"auto", "litang", "nudenet"}
        self._nudenet_wanted = self.wanted in {"auto", "nudenet"}
        self._resolved = self._resolve_backend()
        if self.is_opencv_fallback:
            logger.info(
                "[honzi] nsfw_filter start backend=%s cloud=never "
                "note=OpenCV降级（无理塘/NudeNet 本地权重）；硬drop门槛已抬高，可疑页改 censor",
                self._resolved,
            )
        else:
            logger.info(
                "[honzi] nsfw_filter start backend=%s cloud=never",
                self._resolved,
            )

    @property
    def backend_id(self) -> str:
        return self._resolved

    @property
    def is_local(self) -> bool:
        return True

    @property
    def is_opencv_fallback(self) -> bool:
        """auto/litang/nudenet 想要检测器但实际落到 opencv/heuristic。"""
        return self._detector_wanted and self._resolved in {"opencv", "heuristic"}

    def user_backend_label(self) -> str:
        if self.is_opencv_fallback:
            return f"{self._resolved}（OpenCV 降级，无理塘/NudeNet）"
        if self._resolved == "litang":
            return "litang（理塘打码模型）"
        return self._resolved

    def _resolve_backend(self) -> str:
        if self._detect_fn is not None:
            if self.wanted in {"auto", "litang"}:
                return "litang-mock"
            if self.wanted == "nudenet":
                return "nudenet-mock"
            return self.wanted
        if self.wanted == "heuristic":
            return "heuristic"
        if self.wanted == "opencv":
            return "opencv"
        # auto / litang / nudenet
        if self.wanted in {"auto", "litang"} and self._litang_ready():
            return "litang"
        if self.wanted == "litang":
            logger.warning(
                "[honzi] 理塘 censor.onnx 不可用，降级 nudenet/opencv；不会下载云端模型"
            )
        if self.wanted in {"auto", "nudenet"} and self._nudenet_ready():
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

    def _litang_ready(self) -> bool:
        try:
            from litang_censor import litang_ready, find_litang_model  # type: ignore
        except ImportError:
            try:
                from .litang_censor import litang_ready, find_litang_model  # type: ignore
            except ImportError:
                return False
        explicit = self._litang_model_path or (
            self.model_path if self.model_path.endswith("censor.onnx") else ""
        )
        if not litang_ready(explicit):
            return False
        found = find_litang_model(explicit)
        if found:
            self._litang_model_path = str(found)
        return True

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

    def _litang_detect(self, image_path: str) -> list[dict[str, Any]]:
        if self._detect_fn is not None:
            return list(self._detect_fn(image_path) or [])
        try:
            from litang_censor import detect_litang  # type: ignore
        except ImportError:
            from .litang_censor import detect_litang  # type: ignore
        mp = getattr(self, "_litang_model_path", "") or (
            self.model_path if self.model_path.endswith("censor.onnx") else ""
        )
        return detect_litang(
            image_path,
            model_path=mp,
            conf=0.20,
        )

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
        if self._resolved.startswith("litang"):
            try:
                dets = self._litang_detect(str(path))
                return decide_from_detections(
                    dets,
                    drop_threshold=self.drop_threshold,
                    censor_threshold=self.censor_threshold,
                    backend=self._resolved,
                )
            except CloudVisionForbidden:
                raise
            except Exception as e:
                logger.warning("[honzi] 理塘模型失败，降级 NudeNet/启发式: %s", e)
                if self._nudenet_ready():
                    try:
                        dets = self._nudenet_detect(str(path))
                        return decide_from_detections(
                            dets,
                            drop_threshold=self.drop_threshold,
                            censor_threshold=self.censor_threshold,
                            backend="nudenet",
                        )
                    except Exception as e2:
                        logger.warning("[honzi] NudeNet 亦失败: %s", e2)
                return decide_from_skin_heuristic(path, backend="heuristic")
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
