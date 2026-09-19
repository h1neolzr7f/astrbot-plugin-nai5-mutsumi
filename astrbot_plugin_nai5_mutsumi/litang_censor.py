"""理塘百宝箱 censor.onnx 本地推理（YOLO Ultralytics → ONNX Runtime）。

来源：https://github.com/h1neolzr7f/litang-baibaoxiang Release APK assets/censor.onnx
类：0=nipple_f（censor） / 1=penis（drop） / 2=pussy（drop）
禁止联网；权重只读本地路径。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("astrbot_plugin_nai5_mutsumi.litang_censor")

LITANG_CLASS_NAMES = {0: "nipple_f", 1: "penis", 2: "pussy"}
# 对齐 NudeNet 语义，便于 decide_from_detections 复用
LITANG_TO_FILTER_LABEL = {
    "nipple_f": "FEMALE_BREAST_EXPOSED",
    "penis": "MALE_GENITALIA_EXPOSED",
    "pussy": "FEMALE_GENITALIA_EXPOSED",
}

_IMGSZ = 640
_SESSION_CACHE: dict[str, Any] = {}


def default_litang_paths(explicit: str = "") -> list[Path]:
    env = os.environ.get("LITANG_CENSOR_MODEL_PATH") or ""
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    if env:
        cands.append(Path(env))
    cands.extend(
        [
            Path("/AstrBot/data/plugin_data/astrbot_plugin_nai5_mutsumi/litang/censor.onnx"),
            Path("/AstrBot/data/plugin_data/astrbot_plugin_nai5_mutsumi/censor/censor.onnx"),
            Path(__file__).resolve().parent / "litang" / "censor.onnx",
            Path("/workspace/qqbot-nai/models/litang-censor/censor.onnx"),
        ]
    )
    return cands


def find_litang_model(explicit: str = "") -> Path | None:
    for p in default_litang_paths(explicit):
        if p.is_file() and p.stat().st_size > 1024:
            return p
    return None


def litang_ready(explicit: str = "") -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return find_litang_model(explicit) is not None


def _letterbox(im_bgr, new_shape: int = _IMGSZ, color=(114, 114, 114)):
    """YOLO letterbox：保持比例缩放 + 灰边填充。返回 tensor NCHW float32、ratio、pad(dw,dh)。"""
    import cv2
    import numpy as np

    h0, w0 = im_bgr.shape[:2]
    r = min(new_shape / h0, new_shape / w0)
    new_unpad = (int(round(w0 * r)), int(round(h0 * r)))
    dw = new_shape - new_unpad[0]
    dh = new_shape - new_unpad[1]
    dw /= 2
    dh /= 2
    if (w0, h0) != new_unpad:
        im = cv2.resize(im_bgr, new_unpad, interpolation=cv2.INTER_LINEAR)
    else:
        im = im_bgr
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    blob = im[:, :, ::-1].transpose(2, 0, 1)  # BGR→RGB, HWC→CHW
    blob = np.ascontiguousarray(blob, dtype=np.float32) / 255.0
    blob = blob[None, ...]
    return blob, r, (left, top)


def _nms_xyxy(boxes, scores, iou_thres: float = 0.45) -> list[int]:
    import numpy as np

    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thres]
    return keep


def _get_session(model_path: str):
    key = str(Path(model_path).resolve())
    sess = _SESSION_CACHE.get(key)
    if sess is not None:
        return sess
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2)))
    sess = ort.InferenceSession(key, sess_options=so, providers=["CPUExecutionProvider"])
    _SESSION_CACHE[key] = sess
    logger.info("[litang] loaded censor.onnx path=%s", key)
    return sess


def detect_litang(
    image_path: str | Path,
    *,
    model_path: str = "",
    conf: float = 0.25,
    iou: float = 0.45,
) -> list[dict[str, Any]]:
    """返回与 NudeNet 相近的 detections：class / score / box=[x,y,w,h]。"""
    import cv2
    import numpy as np

    path = Path(image_path)
    mp = find_litang_model(model_path)
    if mp is None:
        raise RuntimeError("litang censor.onnx 未找到（本地路径）")
    img = cv2.imread(str(path))
    if img is None:
        raise RuntimeError(f"无法读取图像: {path}")
    h0, w0 = img.shape[:2]
    blob, ratio, (pad_x, pad_y) = _letterbox(img, _IMGSZ)
    sess = _get_session(str(mp))
    inp = sess.get_inputs()[0].name
    outs = sess.run(None, {inp: blob})
    pred = np.asarray(outs[0])  # [1, 7, 8400]
    if pred.ndim == 3:
        pred = pred[0]
    # [7, 8400] → [8400, 7]
    if pred.shape[0] < pred.shape[1]:
        pred = pred.T
    boxes_xywh = pred[:, :4]
    cls_scores = pred[:, 4:]
    cls_ids = cls_scores.argmax(axis=1)
    scores = cls_scores.max(axis=1)
    mask = scores >= float(conf)
    boxes_xywh = boxes_xywh[mask]
    scores = scores[mask]
    cls_ids = cls_ids[mask]
    if boxes_xywh.size == 0:
        return []

    # cxcywh → xyxy in letterbox space, then undo pad/scale
    cx, cy, bw, bh = boxes_xywh[:, 0], boxes_xywh[:, 1], boxes_xywh[:, 2], boxes_xywh[:, 3]
    x1 = (cx - bw / 2 - pad_x) / ratio
    y1 = (cy - bh / 2 - pad_y) / ratio
    x2 = (cx + bw / 2 - pad_x) / ratio
    y2 = (cy + bh / 2 - pad_y) / ratio
    x1 = np.clip(x1, 0, w0 - 1)
    y1 = np.clip(y1, 0, h0 - 1)
    x2 = np.clip(x2, 0, w0 - 1)
    y2 = np.clip(y2, 0, h0 - 1)
    boxes = np.stack([x1, y1, x2, y2], axis=1)

    keep = _nms_xyxy(boxes, scores, iou_thres=float(iou))
    dets: list[dict[str, Any]] = []
    for i in keep:
        xi1, yi1, xi2, yi2 = [float(v) for v in boxes[i]]
        ww = max(1.0, xi2 - xi1)
        hh = max(1.0, yi2 - yi1)
        raw_name = LITANG_CLASS_NAMES.get(int(cls_ids[i]), str(int(cls_ids[i])))
        label = LITANG_TO_FILTER_LABEL.get(raw_name, raw_name.upper())
        dets.append(
            {
                "class": label,
                "label": label,
                "raw_class": raw_name,
                "score": float(scores[i]),
                "box": [int(xi1), int(yi1), int(ww), int(hh)],
            }
        )
    return dets
