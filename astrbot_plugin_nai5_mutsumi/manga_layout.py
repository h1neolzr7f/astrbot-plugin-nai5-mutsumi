"""漫画分镜中间表示与 NovelAI 坐标映射（纯函数，无 I/O）。

设计目标
========
视觉模型擅长「看见格子在哪」，不擅长记 NovelAI 的字母/数字轴。
1.4.18–1.4.20 用关键词判断竖条再把 A3→C1 这种 remap 当主方案，只能凑合单张样例。

本模块改成：
- LLM 输出 LAYOUT（归一化 bbox / 槽中心），不负责 A1–E5；
- 用与 ppnai ``_pos_to_center`` 互逆的公式把 (cx, cy) 编成 A1–E5；
- 用几何分类版式（叠条 / 网格 / 非对称），再回填 PROMPT；
- 校验对白是否像动作摘要；
- 字母当行的统计转置只作 **无 LAYOUT 时的最后兜底**，并留下日志理由。

NovelAI / ppnai 约定（与 ``ppnai data_source._pos_to_center`` 一致）
----------------------------------------------------------------
- 字母 A–E = 左 → 右；数字 1–5 = 上 → 下。
- 中心：x = (ord(L)-ord('A'))*0.2+0.1，y = (digit-1)*0.2+0.1。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

POS_RE = re.compile(r"^[A-E][1-5]$")
LETTERS = "ABCDE"
N_PANEL_RE = re.compile(r"\b([2-9]|1[0-2])-panel\s+manga\s+page\b", re.I)
WEIGHTED_RE = re.compile(r"^-?\d+(?:\.\d+)?::(.+?)::$")

# 与 ppnai 一致的 5×5 格子中心
def pos_to_center(pos: str) -> tuple[float, float]:
    p = (pos or "C3").upper().strip()
    if len(p) >= 2 and p[0] in LETTERS and p[1].isdigit():
        x = (ord(p[0]) - ord("A")) * 0.2 + 0.1
        y = (int(p[1]) - 1) * 0.2 + 0.1
        return (round(x, 1), round(y, 1))
    return (0.5, 0.5)


def center_to_pos(cx: float, cy: float) -> str:
    """归一化中心 → A1–E5。与 pos_to_center 互逆（四舍五入到最近格）。"""
    try:
        x = float(cx)
        y = float(cy)
    except (TypeError, ValueError):
        return "C3"
    x = min(1.0, max(0.0, x))
    y = min(1.0, max(0.0, y))
    col = int(round((x - 0.1) / 0.2))
    row = int(round((y - 0.1) / 0.2))
    col = min(4, max(0, col))
    row = min(4, max(0, row))
    return f"{LETTERS[col]}{row + 1}"


def clamp01(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(x) or math.isinf(x):
        return default
    return min(1.0, max(0.0, x))


@dataclass
class Panel:
    id: int
    x: float
    y: float
    w: float
    h: float
    shot: str = ""

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)


@dataclass
class LayoutSlot:
    panel: int
    cx: float
    cy: float
    text: str = ""
    kind: str = ""


@dataclass
class MangaLayout:
    panel_count: int
    reading: str = "rtl"
    kind: str = "asymmetric"
    panels: list[Panel] = field(default_factory=list)
    slots: list[LayoutSlot] = field(default_factory=list)
    source: str = "llm"

    def usable(self) -> bool:
        return self.panel_count >= 2 and len(self.panels) >= 2


# ---------------------------------------------------------------------------
# LAYOUT JSON
# ---------------------------------------------------------------------------

_LAYOUT_LABEL_RE = re.compile(r"(?:^|\n)\s*LAYOUT\s*[:：]\s*", re.I)


def _extract_balanced_object(text: str, start: int) -> str | None:
    i = text.find("{", start)
    if i < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    return None


def extract_layout_json(text: str) -> str | None:
    """从 LLM 原文抽出 LAYOUT 对象；兼容标签、围栏、或首个含 panels 的对象。"""
    raw = text or ""
    m = _LAYOUT_LABEL_RE.search(raw)
    if m:
        blob = _extract_balanced_object(raw, m.end())
        if blob:
            return blob
    for fence in re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw, re.I):
        if "panel" in fence.lower() or '"slots"' in fence or '"n"' in fence:
            blob = _extract_balanced_object(fence, 0)
            if blob:
                return blob
    # 兜底：全文第一个像分镜的对象
    idx = 0
    while True:
        i = raw.find("{", idx)
        if i < 0:
            return None
        blob = _extract_balanced_object(raw, i)
        if blob and re.search(r'"(panels|panel_count|slots|n)"\s*:', blob):
            return blob
        idx = i + 1


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _read_reading(v: Any) -> str:
    s = str(v or "rtl").strip().lower()
    if s in {"ltr", "left-to-right", "left_to_right", "l2r"}:
        return "ltr"
    return "rtl"


def parse_layout_dict(data: dict[str, Any]) -> MangaLayout | None:
    if not isinstance(data, dict):
        return None
    panels_raw = data.get("panels") or data.get("panel") or []
    if not isinstance(panels_raw, list):
        return None
    panels: list[Panel] = []
    for i, item in enumerate(panels_raw, start=1):
        if not isinstance(item, dict):
            continue
        pid = _as_int(item.get("id", item.get("n", i)), i)
        x = clamp01(item.get("x", item.get("left", 0.0)))
        y = clamp01(item.get("y", item.get("top", 0.0)))
        w = clamp01(item.get("w", item.get("width", 0.0)), 0.0)
        h = clamp01(item.get("h", item.get("height", 0.0)), 0.0)
        if w <= 0.02 or h <= 0.02:
            # 允许只给中心+粗略尺寸
            cx = item.get("cx")
            cy = item.get("cy")
            if cx is not None and cy is not None:
                w = w if w > 0.02 else 0.2
                h = h if h > 0.02 else 0.2
                x = clamp01(float(cx) - w / 2)
                y = clamp01(float(cy) - h / 2)
            else:
                continue
        shot = str(item.get("shot", item.get("framing", "")) or "").strip()
        panels.append(Panel(id=pid, x=x, y=y, w=min(1.0, w), h=min(1.0, h), shot=shot))
    if len(panels) < 2:
        return None
    n = _as_int(data.get("panel_count", data.get("n", len(panels))), len(panels))
    n = max(n, len(panels))
    slots_raw = data.get("slots") or data.get("characters") or []
    slots: list[LayoutSlot] = []
    if isinstance(slots_raw, list):
        for item in slots_raw:
            if not isinstance(item, dict):
                continue
            pid = _as_int(item.get("panel", item.get("p", item.get("id", 0))), 0)
            cx = item.get("cx", item.get("x"))
            cy = item.get("cy", item.get("y"))
            if cx is None or cy is None:
                pan = next((p for p in panels if p.id == pid), None)
                if pan is None:
                    continue
                cx, cy = pan.cx, pan.cy
            text = str(item.get("text", item.get("dialogue", "")) or "").strip()
            kind = str(item.get("kind", item.get("tk", item.get("text_kind", ""))) or "").strip().lower()
            slots.append(
                LayoutSlot(
                    panel=pid or 1,
                    cx=clamp01(cx, 0.5),
                    cy=clamp01(cy, 0.5),
                    text=text,
                    kind=kind,
                )
            )
    if not slots:
        slots = [
            LayoutSlot(panel=p.id, cx=p.cx, cy=p.cy, text="", kind="") for p in panels
        ]
    kind = str(data.get("kind", data.get("layout", "")) or "").strip().lower()
    geom_kind = classify_layout_kind(panels)
    if kind not in {"stacked_strips", "stacked_columns", "grid", "asymmetric"}:
        kind = geom_kind
    elif kind != geom_kind and geom_kind != "asymmetric":
        # 几何与自称冲突时以几何为准（避免「asymmetric」糊弄竖条）
        kind = geom_kind
    return MangaLayout(
        panel_count=n,
        reading=_read_reading(data.get("reading", data.get("read", "rtl"))),
        kind=kind,
        panels=sorted(panels, key=lambda p: (round(p.y, 3), round(p.x, 3))),
        slots=slots,
        source="llm",
    )


def parse_layout_block(text: str) -> MangaLayout | None:
    blob = extract_layout_json(text)
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", blob)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    return parse_layout_dict(data)


# ---------------------------------------------------------------------------
# 几何分类
# ---------------------------------------------------------------------------

def classify_layout_kind(panels: list[Panel]) -> str:
    """用 bbox 重叠/跨度判断版式，不看关键词。"""
    if len(panels) < 2:
        return "asymmetric"
    wide = [p for p in panels if p.w >= 0.78]
    tall = [p for p in panels if p.h >= 0.78]
    if len(wide) == len(panels) and _mostly_stacked_y(panels):
        return "stacked_strips"
    if len(tall) == len(panels) and _mostly_stacked_x(panels):
        return "stacked_columns"
    rows, cols = _estimate_grid(panels)
    if rows >= 2 and cols >= 2 and abs(rows * cols - len(panels)) <= 1:
        areas = [p.area for p in panels]
        mean = sum(areas) / len(areas)
        if max(areas) <= 1.8 * mean and max(areas) <= 2.2 * min(areas):
            return "grid"
    return "asymmetric"


def _mostly_stacked_y(panels: list[Panel]) -> bool:
    ordered = sorted(panels, key=lambda p: p.y)
    for a, b in zip(ordered, ordered[1:]):
        # 横向几乎同宽同列，纵向错开
        if abs(a.x - b.x) > 0.25:
            return False
        if b.y + 0.02 < a.y + a.h * 0.35:
            return False
    return True


def _mostly_stacked_x(panels: list[Panel]) -> bool:
    ordered = sorted(panels, key=lambda p: p.x)
    for a, b in zip(ordered, ordered[1:]):
        if abs(a.y - b.y) > 0.25:
            return False
        if b.x + 0.02 < a.x + a.w * 0.35:
            return False
    return True


def _estimate_grid(panels: list[Panel]) -> tuple[int, int]:
    ys = sorted({round(p.cy * 10) / 10 for p in panels})
    xs = sorted({round(p.cx * 10) / 10 for p in panels})
    return (max(1, len(ys)), max(1, len(xs)))


def geometry_sentence(layout: MangaLayout) -> str:
    """由 bbox 生成 PROMPT 几何短句（无角色/台词）。"""
    n = layout.panel_count
    kind = layout.kind or classify_layout_kind(layout.panels)
    if kind == "stacked_strips":
        return f"{n} full-width horizontal strips stacked top to bottom"
    if kind == "stacked_columns":
        return f"{n} full-height vertical columns arranged left to right"
    if kind == "grid":
        rows, cols = _estimate_grid(layout.panels)
        return f"{rows}x{cols} manga panel grid"
    bits: list[str] = []
    for p in layout.panels:
        bits.append(f"{_region_name(p)} {_size_name(p)} panel")
    return "asymmetric comic layout: " + "; ".join(bits)


def _region_name(p: Panel) -> str:
    cx, cy = p.cx, p.cy
    vert = "top" if cy < 0.33 else ("bottom" if cy > 0.66 else "middle")
    horz = "left" if cx < 0.33 else ("right" if cx > 0.66 else "center")
    if p.w >= 0.78:
        return vert
    if p.h >= 0.78:
        return horz
    return f"{vert}-{horz}"


def _size_name(p: Panel) -> str:
    if p.area >= 0.32:
        return "large"
    if p.area <= 0.10:
        return "small"
    return "medium"


# ---------------------------------------------------------------------------
# 格位分配
# ---------------------------------------------------------------------------

def assign_positions_from_slots(slots: list[LayoutSlot]) -> list[str]:
    """槽中心 → A1–E5；同格碰撞优先同数字、拆字母。"""
    raw = [center_to_pos(s.cx, s.cy) for s in slots]
    used: set[str] = set()
    out: list[str] = []
    for i, pos in enumerate(raw):
        slot = slots[i]
        same_panel = [
            j for j, other in enumerate(slots) if other.panel == slot.panel
        ]
        if len(same_panel) >= 2:
            digit = pos[1]
            # 按 cx 从左到右分配字母，行号取该格中心行
            group_cx = [(j, slots[j].cx) for j in same_panel]
            group_cx.sort(key=lambda t: t[1])
            letters = _spread_letters(len(group_cx))
            rank = next(k for k, (j, _) in enumerate(group_cx) if j == i)
            pos = f"{letters[rank]}{digit}"
        pos = _unique_pos(pos, used, prefer_row=pos[1] if POS_RE.match(pos) else "3")
        used.add(pos)
        out.append(pos)
    return out


def _spread_letters(n: int) -> list[str]:
    """把 n 人沿水平轴摊开：1→C，2→B/D，3→B/C/D，4→A/B/D/E，5→A–E。"""
    if n <= 1:
        return ["C"]
    if n == 2:
        return ["B", "D"]
    if n == 3:
        return ["B", "C", "D"]
    if n == 4:
        return ["A", "B", "D", "E"]
    return list(LETTERS[:n])


def _unique_pos(pos: str, used: set[str], prefer_row: str = "3") -> str:
    if POS_RE.match(pos or "") and pos not in used:
        return pos
    row = prefer_row if prefer_row in "12345" else (pos[1] if POS_RE.match(pos or "") else "3")
    col = pos[0] if POS_RE.match(pos or "") else "C"
    for letter in (col, "B", "D", "C", "A", "E"):
        cand = f"{letter}{row}"
        if cand not in used:
            return cand
    for r in "12345":
        for letter in LETTERS:
            cand = f"{letter}{r}"
            if cand not in used:
                return cand
    return pos or "C3"


def apply_layout_to_chars(
    chars: list[dict[str, str]], layout: MangaLayout
) -> list[dict[str, str]]:
    """用 LAYOUT 几何覆盖 CHARACTER 的 position；不改身份 tag。"""
    if not layout.usable():
        return chars
    slots = list(layout.slots)
    if len(chars) > len(slots):
        extras = _extra_slots_from_panels(layout, len(chars) - len(slots))
        slots = slots + extras
    positions = assign_positions_from_slots(slots[: max(len(chars), 1)])
    out: list[dict[str, str]] = []
    for i, c in enumerate(chars):
        cc = dict(c)
        if i < len(positions):
            cc["position"] = positions[i]
        body = cc.get("prompt") or ""
        if i < len(slots):
            body = merge_slot_dialogue(body, slots[i])
        cc["prompt"] = body
        out.append(cc)
    return out


def _extra_slots_from_panels(layout: MangaLayout, need: int) -> list[LayoutSlot]:
    extra: list[LayoutSlot] = []
    panels = reading_ordered_panels(layout)
    for p in panels:
        if len(extra) >= need:
            break
        extra.append(LayoutSlot(panel=p.id, cx=p.cx, cy=p.cy))
    while len(extra) < need:
        extra.append(LayoutSlot(panel=1, cx=0.5, cy=0.5))
    return extra


def reading_ordered_panels(layout: MangaLayout) -> list[Panel]:
    rtl = layout.reading != "ltr"

    def key(p: Panel) -> tuple[float, float]:
        row = round(p.y, 2)
        col = -p.x if rtl else p.x
        return (row, col)

    return sorted(layout.panels, key=key)


# ---------------------------------------------------------------------------
# 对白校验
# ---------------------------------------------------------------------------

# 动作/景别词：用于识别「text 里写的是摘要不是台词」。多语种词元，不是某张样例对白。
_ACTION_LEMMAS_EN = {
    "sitting", "standing", "kneeling", "lying", "walking", "running",
    "looking", "holding", "leaning", "kiss", "kissing", "hug", "hugging",
    "eyes closed", "eye closed", "closed eyes", "close-up", "closeup",
    "upper body", "full body", "from side", "from behind", "profile",
    "blush", "blushing", "smile", "crying", "waving", "pointing",
    "reaching", "hand", "hands", "finger", "facing viewer",
}
_ACTION_LEMMAS_ZH = {
    "闭眼", "睁眼", "伸手", "握手", "举手", "转头", "回头", "低头", "抬头",
    "侧身", "站立", "坐下", "坐下", "跪着", "躺着", "奔跑", "走路",
    "拥抱", "接吻", "亲吻", "微笑", "哭泣", "脸红", "特写", "远景",
    "看向", "看镜头", "侧视", "背影", "抬腿", "抬手",
}
_ZH_PARTICLE_RE = re.compile(r"[的了吗呢吧啊呀哦嗯嘛啦哇…！？。，、]")
_TEXT_TAG_RE = re.compile(r'(?i)\btext:\s*"([^"]*)"')


def is_action_summary_text(text: str) -> bool:
    """判断引号内容是否像动作/景别摘要，而不是一句对白。"""
    t = (text or "").strip().strip("“”\"'")
    if not t:
        return True
    low = t.lower()
    if low in _ACTION_LEMMAS_EN or t in _ACTION_LEMMAS_ZH:
        return True
    if any(low == x or low.startswith(x + " ") for x in _ACTION_LEMMAS_EN):
        return True
    # 极短、无语气词的中文动词短语
    cjk = re.findall(r"[\u4e00-\u9fff]", t)
    if 1 <= len(cjk) <= 4 and len(t) <= 6 and not _ZH_PARTICLE_RE.search(t):
        if any(k in t for k in _ACTION_LEMMAS_ZH):
            return True
        if re.fullmatch(r"[\u4e00-\u9fff]{1,4}", t):
            # 单字/两字且像动词（闭/看/伸/站/坐/抱/吻/走…）
            if t[0] in "闭看伸站坐抱吻走跑躺跪转低抬握举贴靠":
                return True
    if re.fullmatch(r"[a-z][a-z \-]{0,18}", low) and any(
        w in low.split() for w in _ACTION_LEMMAS_EN
    ):
        return True
    return False


def merge_slot_dialogue(prompt: str, slot: LayoutSlot) -> str:
    """把 LAYOUT 原句写入 text:"…"；丢掉动作摘要冒充的台词。"""
    body = prompt or ""
    quoted = slot.text.strip()
    existing = _TEXT_TAG_RE.findall(body)
    if quoted and is_action_summary_text(quoted):
        quoted = ""
    keep_existing = [q for q in existing if q.strip() and not is_action_summary_text(q)]
    if quoted:
        # LAYOUT 原文优先
        if not any(q == quoted for q in keep_existing):
            kind = slot.kind
            prefix = ""
            if kind in {"narration", "box", "narration_box"}:
                prefix = "rectangular narration box, "
            elif kind in {"bubble", "speech", "speech_bubble"} or True:
                if "speech bubble" not in body.lower() and kind != "narration":
                    prefix = "speech bubble, "
            body = _TEXT_TAG_RE.sub("", body)
            body = re.sub(r",\s*,", ", ", body).strip(" ,")
            tag = f'{prefix}text: "{quoted}"'
            body = (body + ", " + tag).strip(" ,")
        return _tidy_commas(body)
    # 无原文：剔除槽内动作摘要 text
    def _drop_bad(m: re.Match) -> str:
        return "" if is_action_summary_text(m.group(1)) else m.group(0)

    body = _TEXT_TAG_RE.sub(_drop_bad, body)
    return _tidy_commas(body)


def sanitize_chars_dialogue(chars: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for c in chars:
        cc = dict(c)
        body = cc.get("prompt") or ""

        def _drop_bad(m: re.Match) -> str:
            return "" if is_action_summary_text(m.group(1)) else m.group(0)

        cc["prompt"] = _tidy_commas(_TEXT_TAG_RE.sub(_drop_bad, body))
        out.append(cc)
    return out


def _tidy_commas(s: str) -> str:
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r",\s*,+", ", ", s)
    return s.strip(" ,")


# ---------------------------------------------------------------------------
# PROMPT 与 LAYOUT 对齐
# ---------------------------------------------------------------------------

_VAGUE_LAYOUT_TAGS = {
    "comic page",
    "manga page",
    "multiple panels",
    "multi-panel",
    "multi-panel manga page",
    "asymmetric multi-panel layout",
    "speech bubble",
}

_SHOT_WORDS = (
    "close-up", "closeup", "extreme close-up", "upper body", "cowboy shot",
    "full body", "medium shot", "portrait", "hands", "wide shot",
    "establishing", "insert",
)
_PANEL_ACTION_RE = re.compile(
    r"(?i)((?:top|bottom|left|right|second|third|fourth|fifth|1st|2nd|3rd|\d+(?:st|nd|rd|th))\s+)?panel\s*:"
    r"[^,]{0,80}\b(looking|holding|sitting|standing|kiss|leaning|hand|eye|blush|closed)\b[^,]*"
)


def strip_weight_core(token: str) -> str:
    s = (token or "").strip()
    m = WEIGHTED_RE.match(s)
    return (m.group(1) if m else s).strip()


def split_prompt_parts(prompt: str) -> list[str]:
    return [x.strip() for x in (prompt or "").split(",") if x.strip()]


def merge_prompt_with_layout(prompt: str, layout: MangaLayout | None) -> str:
    """保留光影/质量，用 LAYOUT 覆盖格数与几何；去掉糊弄 tag 与剧情泄漏。"""
    parts = split_prompt_parts(prompt)
    kept: list[str] = []
    has_npanel = False
    has_dir = False
    n = layout.panel_count if layout and layout.usable() else 0
    for raw in parts:
        core = strip_weight_core(raw).lower()
        m = N_PANEL_RE.search(core)
        if m:
            got = int(m.group(1))
            if n and got != n:
                continue
            has_npanel = True
            kept.append(raw if WEIGHTED_RE.match(raw) else f"1.40::{core}::")
            continue
        if core in _VAGUE_LAYOUT_TAGS:
            continue
        if core in {"right-to-left", "left-to-right"}:
            has_dir = True
            kept.append(raw)
            continue
        if _PANEL_ACTION_RE.search(raw):
            continue
        kept.append(raw)
    head: list[str] = []
    if n:
        if not has_npanel:
            head.append(f"1.40::{n}-panel manga page::")
        if layout:
            want_dir = "right-to-left" if layout.reading != "ltr" else "left-to-right"
            if not has_dir:
                head.append(f"1.35::{want_dir}::")
            geo = geometry_sentence(layout)
            if geo and geo.lower() not in {strip_weight_core(p).lower() for p in kept}:
                head.append(geo)
            if "clean panel borders" not in {strip_weight_core(p).lower() for p in kept}:
                head.append("1.35::clean panel borders::")
            if "white gutter" not in {strip_weight_core(p).lower() for p in kept}:
                head.append("1.35::white gutter::")
    elif not has_npanel:
        head.append("1.40::multi-panel manga page::")
    merged = head + kept
    seen: set[str] = set()
    out: list[str] = []
    for item in merged:
        key = strip_weight_core(item).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return ", ".join(out)


def extract_n_panel(prompt: str) -> int | None:
    m = N_PANEL_RE.search(prompt or "")
    if not m:
        return None
    return int(m.group(1))


def layout_is_weak(
    layout: MangaLayout | None,
    prompt: str,
    chars: list[dict[str, str]],
) -> bool:
    """结构级弱分镜：缺几何、格数对不上、槽过少。"""
    if layout is None or not layout.usable():
        # 漫画反推缺几何就视为弱，走一次「请补 LAYOUT」修复
        return True
    if layout.panel_count < 2:
        return True
    if abs(layout.panel_count - len(layout.panels)) > 1:
        return True
    if len(chars) < 1:
        return True
    n_in_prompt = extract_n_panel(prompt)
    if n_in_prompt is not None and abs(n_in_prompt - layout.panel_count) >= 2:
        return True
    return False


# ---------------------------------------------------------------------------
# 无 LAYOUT 时的统计兜底（非主路径）
# ---------------------------------------------------------------------------

def looks_like_letter_as_row(positions: Iterable[str]) -> bool:
    """高置信：字母跨度像「行」、数字挤在中间行 → 轴可能反了。

    不看 PROMPT 关键词。3 格横条 A3/C3/E3 与真·从左到右无法区分，故要求
    更强信号（≥5 个不同字母，或 4 字母且数字种类 ≤2）。
    """
    valid = [p.upper() for p in positions if POS_RE.match((p or "").upper())]
    if len(valid) < 3:
        return False
    letters = [p[0] for p in valid]
    digits = [p[1] for p in valid]
    u_let, u_dig = len(set(letters)), len(set(digits))
    if u_let >= 5 and u_dig <= 3:
        return True
    if u_let >= 4 and u_dig <= 2:
        return True
    if u_let >= 3 and u_dig == 1 and digits[0] == "3":
        # 全挤在第 3 行、字母当序号：对「竖条被标成 A3…E3」高召回
        # 但对真·一行三人会误伤，故只在调用方确认「无 LAYOUT」且格数≥4 时使用
        return len(valid) >= 4
    return False


def transpose_letter_row_positions(
    chars: list[dict[str, str]],
) -> list[dict[str, str]]:
    """兜底：把「字母当行、数字当列」转回官方轴。A3→C1, E3→C5。"""
    letter_row = {ch: i + 1 for i, ch in enumerate(LETTERS)}
    digit_col = {i + 1: ch for i, ch in enumerate(LETTERS)}
    used: set[str] = set()
    out: list[dict[str, str]] = []
    for c in chars:
        cc = dict(c)
        pos = (cc.get("position") or "C3").upper().strip()
        if POS_RE.match(pos):
            row = letter_row[pos[0]]
            col = digit_col.get(int(pos[1]), "C")
            new_pos = f"{col}{row}"
        else:
            new_pos = "C3"
        new_pos = _unique_pos(new_pos, used, prefer_row=new_pos[1])
        used.add(new_pos)
        cc["position"] = new_pos
        out.append(cc)
    return out


def ensure_distinct_manga_positions(
    chars: list[dict[str, str]],
) -> list[dict[str, str]]:
    """漫画：撞车时优先同数字换字母，避免把竖条拆到别的行。"""
    used: set[str] = set()
    out: list[dict[str, str]] = []
    for c in chars:
        cc = dict(c)
        pos = (cc.get("position") or "").upper().strip()
        if not POS_RE.match(pos) or pos in used:
            row = pos[1] if POS_RE.match(pos) else "3"
            pos = _unique_pos(pos if POS_RE.match(pos) else "C3", used, prefer_row=row)
        cc["position"] = pos
        used.add(pos)
        out.append(cc)
    return out


def manga_size_for_layout(size: str, layout: MangaLayout | None) -> str:
    """多格页默认方图，避免竖图把横条压扁。"""
    cur = (size or "").strip().lower()
    if cur in {"", "768x1024", "512x768", "640x896"}:
        return "1024x1024"
    return size or "1024x1024"


def diagnose_positions(positions: list[str]) -> dict[str, Any]:
    valid = [p.upper() for p in positions if POS_RE.match((p or "").upper())]
    letters = [p[0] for p in valid]
    digits = [p[1] for p in valid]
    return {
        "count": len(valid),
        "unique_letters": len(set(letters)),
        "unique_digits": len(set(digits)),
        "letters": letters,
        "digits": digits,
        "axis_swap_suspect": looks_like_letter_as_row(valid),
    }
