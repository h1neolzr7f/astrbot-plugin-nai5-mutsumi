"""nai5本子 指令解析（纯函数，无 I/O）。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

HonziKind = Literal["start", "cancel", "status", "unknown"]

_CMD_HEAD = (
    r"nai5画本子批处理|nai5本子批处理|"
    r"nai5画本子|mutsumi画本子|nai5本子|睦画本子"
)
_HONZI_RE = re.compile(
    rf"(?is)^[/!！]?(?P<cmd>{_CMD_HEAD})(?P<tail>.*)$"
)
_CANCEL_RE = re.compile(
    r"(?is)^[:：\s]*(取消|停止|stop|cancel|abort)(?:本子)?\s*$"
)
_STATUS_RE = re.compile(
    r"(?is)^[:：\s]*(进度|状态|查询|status|progress)(?:\s*查询)?\s*$"
)


@dataclass(frozen=True)
class HonziCommand:
    kind: HonziKind
    requirement: str = ""
    raw: str = ""

    @property
    def is_honzi(self) -> bool:
        return self.kind != "unknown"


def strip_at_noise(text: str) -> str:
    s = text or ""
    s = re.sub(r"\[CQ:at,[^\]]+\]", " ", s)
    s = re.sub(r"@\S+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_honzi_command(text: str) -> HonziCommand:
    """解析 nai5本子 / 取消 / 进度。不匹配则 kind=unknown。"""
    raw = strip_at_noise(text)
    m = _HONZI_RE.match(raw)
    if not m:
        return HonziCommand(kind="unknown", requirement=raw, raw=raw)
    tail = (m.group("tail") or "").strip()
    tail = tail.lstrip("：:").strip()
    if _CANCEL_RE.match(tail):
        return HonziCommand(kind="cancel", requirement="", raw=raw)
    if _STATUS_RE.match(tail):
        return HonziCommand(kind="status", requirement="", raw=raw)
    return HonziCommand(kind="start", requirement=tail, raw=raw)


def is_honzi_text(text: str) -> bool:
    return parse_honzi_command(text).is_honzi
