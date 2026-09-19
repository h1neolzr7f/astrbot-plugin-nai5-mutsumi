"""定位 / 解压 JM 本子 zip（不写死单一 album）。

查找顺序：
1. 引用消息 / 正文里的本子 ID、文件名、File 路径
2. 本会话最近一次成功解析的 zip（sidecar JSON）
3. 该用户最近一次
4. download_dir 里按 mtime 最新、且文件名对得上 hint 的 zip
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_ZIP_PASSWORD = "dickding"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# 显式：jm 123 / jm 12576 / jmi 10010 / 本子 12576（JM ID 最短可 3 位）
_EXPLICIT_ID_RE = re.compile(
    r"(?i)(?:(?:^|[^\w])jm(?:c|i|s|update)?\s*[/:]?\s*|本子\s*(?:id\s*)?)"
    r"(\d{3,12})"
)
_BRACKET_ID_RE = re.compile(r"[【\[\(](\d{3,12})[】\]\)]")
# 文件名里独立数字段（避免把 2024 年当 ID：要求 ≥5 位或带密码后缀）
_FNAME_ID_RE = re.compile(
    r"(?i)(?:^|[_\-\s\[\(])(\d{5,12})(?:[_\-\s\.\]\)]|$|dickding)"
)
# jm_cosmos 打包名：{album_id}_{unix_ts}.zip 或 {album_id}_ChN_{unix_ts}.zip
_JM_PACK_NAME_RE = re.compile(
    r"(?i)^(\d{1,12})_(?:[Cc]h\d+_)?(\d{9,11})(?:#PW[^.]*)?(?:\.zip)?$"
)
_PASSWORD_IN_NAME_RE = re.compile(r"(?i)dickding")


def _looks_like_unix_ts(s: str) -> bool:
    try:
        n = int(s)
    except ValueError:
        return False
    # 约 2001–2033；覆盖当前样例 1789794672（2026）
    return 1_000_000_000 <= n <= 2_000_000_000


@dataclass(frozen=True)
class ZipHint:
    album_ids: tuple[str, ...] = ()
    filenames: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    raw_text: str = ""


@dataclass
class ZipCandidate:
    path: Path
    mtime: float
    album_id: str = ""
    size: int = 0
    source: str = ""


@dataclass
class SessionZipRecord:
    session_key: str
    user_id: str
    path: str
    album_id: str = ""
    mtime: float = 0.0


def parse_album_ids(text: str) -> list[str]:
    """从任意文本抽出本子 ID，不绑定某一本。"""
    s = text or ""
    found: list[str] = []

    def _add(aid: str) -> None:
        if aid and aid not in found:
            found.append(aid)

    # 优先识别 jm_cosmos 打包名，避免把 unix 时间戳当成 album_id
    for token in re.findall(r"[\w.#-]+\.zip|[\w.#-]+", s, flags=re.I):
        base = Path(token).name
        m = _JM_PACK_NAME_RE.match(base)
        if m and _looks_like_unix_ts(m.group(2)):
            _add(m.group(1))

    for rx in (_EXPLICIT_ID_RE, _BRACKET_ID_RE, _FNAME_ID_RE):
        for m in rx.finditer(s):
            aid = m.group(1)
            if _looks_like_unix_ts(aid):
                continue
            _add(aid)
    # 若上面全被过滤空了，再回退放行 fname 数字（兼容纯时间戳误命名）
    if not found:
        for rx in (_EXPLICIT_ID_RE, _BRACKET_ID_RE, _FNAME_ID_RE):
            for m in rx.finditer(s):
                _add(m.group(1))
    return found


def parse_zip_hints(*texts: str, extra_paths: Iterable[str] = ()) -> ZipHint:
    album_ids: list[str] = []
    filenames: list[str] = []
    paths: list[str] = []
    raw_parts: list[str] = []
    for t in texts:
        if not t:
            continue
        raw_parts.append(t)
        for aid in parse_album_ids(t):
            if aid not in album_ids:
                album_ids.append(aid)
        for m in re.finditer(r"([\w.\-\[\]]+\.zip)", t, flags=re.I):
            name = m.group(1)
            if name not in filenames:
                filenames.append(name)
        for m in re.finditer(r"((?:/[^\s]+)+\.zip)", t, flags=re.I):
            p = m.group(1)
            if p not in paths:
                paths.append(p)
    for p in extra_paths:
        if not p:
            continue
        paths.append(p)
        filenames.append(Path(p).name)
        for aid in parse_album_ids(Path(p).name):
            if aid not in album_ids:
                album_ids.append(aid)
    return ZipHint(
        album_ids=tuple(album_ids),
        filenames=tuple(filenames),
        paths=tuple(paths),
        raw_text="\n".join(raw_parts),
    )


def default_download_dirs(extra: str | Path | None = None) -> list[Path]:
    env = os.environ.get("JM_DOWNLOAD_DIR") or os.environ.get("NAI5_HONZI_DOWNLOAD_DIR")
    cands: list[Path] = []
    if extra:
        cands.append(Path(extra))
    if env:
        cands.append(Path(env))
    cands.extend(
        [
            Path("/AstrBot/data/plugin_data/jm_cosmos2/downloads"),
            Path("/AstrBot/data/plugin_data/astrbot_plugin_jm_cosmos/downloads"),
            Path("/AstrBot/data/plugins/astrbot_plugin_jm_cosmos/downloads"),
            Path("/AstrBot/data/plugins/jm_cosmos2/downloads"),
            Path("./downloads"),
        ]
    )
    out: list[Path] = []
    seen: set[str] = set()
    for p in cands:
        try:
            rp = str(p.expanduser())
        except Exception:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        out.append(Path(rp))
    return out


def album_id_from_name(name: str) -> str:
    base = Path(name or "").name
    m = _JM_PACK_NAME_RE.match(base)
    if m and _looks_like_unix_ts(m.group(2)):
        return m.group(1)
    ids = parse_album_ids(base)
    return ids[0] if ids else ""


def scan_zip_dir(root: Path) -> list[ZipCandidate]:
    if not root.is_dir():
        return []
    found: list[ZipCandidate] = []
    try:
        entries = list(root.rglob("*.zip"))
    except OSError:
        return []
    for p in entries:
        try:
            if not p.is_file():
                continue
            st = p.stat()
            found.append(
                ZipCandidate(
                    path=p,
                    mtime=st.st_mtime,
                    album_id=album_id_from_name(p.name),
                    size=st.st_size,
                    source=str(root),
                )
            )
        except OSError:
            continue
    found.sort(key=lambda c: c.mtime, reverse=True)
    return found


def score_candidate(c: ZipCandidate, hint: ZipHint) -> int:
    score = 0
    name = c.path.name.lower()
    if c.album_id and c.album_id in hint.album_ids:
        score += 100
    for aid in hint.album_ids:
        if aid and name.startswith(f"{aid.lower()}_"):
            score += 90
    for fn in hint.filenames:
        if fn.lower() == name:
            score += 80
        elif fn.lower() in name:
            score += 40
    for hp in hint.paths:
        try:
            if Path(hp).resolve() == c.path.resolve():
                score += 120
        except Exception:
            if hp == str(c.path):
                score += 120
    if _PASSWORD_IN_NAME_RE.search(c.path.name):
        score += 5
    # 新近文件略加分，但不压过 ID 命中
    score += min(10, int(max(0.0, c.mtime) % 100000) and 1)
    return score


def pick_best_zip(
    candidates: Iterable[ZipCandidate],
    hint: ZipHint | None = None,
    *,
    last: SessionZipRecord | None = None,
) -> ZipCandidate | None:
    hint = hint or ZipHint()
    items = list(candidates)
    # 引用路径直接存在
    for hp in hint.paths:
        p = Path(hp)
        if p.is_file() and p.suffix.lower() == ".zip":
            return ZipCandidate(
                path=p,
                mtime=p.stat().st_mtime,
                album_id=album_id_from_name(p.name),
                size=p.stat().st_size,
                source="hint_path",
            )
    if last and last.path:
        lp = Path(last.path)
        if lp.is_file():
            last_c = ZipCandidate(
                path=lp,
                mtime=last.mtime or lp.stat().st_mtime,
                album_id=last.album_id or album_id_from_name(lp.name),
                size=lp.stat().st_size,
                source="session_last",
            )
            # 无明确 ID/文件名时优先会话最近产物
            if not hint.album_ids and not hint.filenames and not hint.paths:
                return last_c
            items.append(last_c)
    if not items:
        return None
    ranked = sorted(
        items,
        key=lambda c: (score_candidate(c, hint), c.mtime),
        reverse=True,
    )
    return ranked[0]


def resolve_zip(
    hint: ZipHint,
    *,
    search_dirs: Iterable[Path] | None = None,
    last: SessionZipRecord | None = None,
    extra_dir: str | Path | None = None,
) -> ZipCandidate | None:
    dirs = list(search_dirs) if search_dirs is not None else default_download_dirs(extra_dir)
    cands: list[ZipCandidate] = []
    for d in dirs:
        cands.extend(scan_zip_dir(Path(d)))
    return pick_best_zip(cands, hint, last=last)


class SessionZipIndex:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            self._data = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = raw if isinstance(raw, dict) else {}
        except Exception:
            self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, session_key: str) -> SessionZipRecord | None:
        row = self._data.get(session_key)
        if not isinstance(row, dict):
            return None
        try:
            return SessionZipRecord(
                session_key=session_key,
                user_id=str(row.get("user_id") or ""),
                path=str(row.get("path") or ""),
                album_id=str(row.get("album_id") or ""),
                mtime=float(row.get("mtime") or 0),
            )
        except Exception:
            return None

    def get_user(self, user_id: str) -> SessionZipRecord | None:
        uid = str(user_id or "").strip()
        if not uid:
            return None
        best: SessionZipRecord | None = None
        for key, row in self._data.items():
            if not isinstance(row, dict):
                continue
            if str(row.get("user_id") or "") != uid:
                continue
            rec = self.get(key)
            if rec is None:
                continue
            if best is None or rec.mtime >= best.mtime:
                best = rec
        return best

    def put(self, rec: SessionZipRecord) -> None:
        self._data[rec.session_key] = asdict(rec)
        # 同步一份用户维度，方便跨群找最近
        user_key = f"u:{rec.user_id}"
        if rec.user_id:
            self._data[user_key] = asdict(rec)
        self._save()


def natural_key(name: str) -> list[Any]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_page_images(root: Path) -> list[Path]:
    pages: list[Path] = []
    if not root.exists():
        return pages
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in IMAGE_EXTS:
            continue
        # 跳过常见非页：macOS junk / 极小图标
        try:
            if p.stat().st_size < 8_000:
                continue
        except OSError:
            continue
        if p.name.startswith(".") or "__MACOSX" in p.parts:
            continue
        pages.append(p)
    pages.sort(key=lambda x: natural_key(str(x.relative_to(root))))
    return pages


def _try_extract_zipfile(zf_path: Path, dest: Path, password: str) -> None:
    with zipfile.ZipFile(zf_path) as zf:
        pwd = password.encode("utf-8") if password else None
        # 有的包无加密
        try:
            zf.extractall(dest, pwd=pwd)
            return
        except RuntimeError:
            if not password:
                raise
        try:
            zf.extractall(dest, pwd=password.encode("gbk", errors="ignore"))
        except Exception:
            zf.extractall(dest)


def _try_extract_pyzipper(zf_path: Path, dest: Path, password: str) -> None:
    import pyzipper  # type: ignore

    with pyzipper.AESZipFile(zf_path) as zf:
        if password:
            zf.pwd = password.encode("utf-8")
        zf.extractall(dest)


def extract_zip_pages(
    zip_path: str | Path,
    dest: str | Path | None = None,
    *,
    password: str = DEFAULT_ZIP_PASSWORD,
) -> tuple[Path, list[Path]]:
    """解压 zip，返回 (工作目录, 页图路径)。调用方负责清理 dest。"""
    zpath = Path(zip_path)
    if not zpath.is_file():
        raise FileNotFoundError(str(zpath))
    work = Path(dest) if dest else Path(tempfile.mkdtemp(prefix="nai5_honzi_"))
    work.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for fn in (_try_extract_zipfile, _try_extract_pyzipper):
        try:
            fn(zpath, work, password)
            last_err = None
            break
        except ImportError:
            continue
        except Exception as e:
            last_err = e
            # 清掉半解压再试下一种
            for child in work.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass
            continue
    if last_err is not None:
        raise last_err
    pages = list_page_images(work)
    if not pages:
        raise FileNotFoundError(f"zip 内没有可用页图: {zpath}")
    return work, pages
