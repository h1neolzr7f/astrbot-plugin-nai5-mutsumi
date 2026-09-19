"""Per-group NAI5 successful-generation quota (SQLite)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_nai5_mutsumi"
DEFAULT_DB = f"/AstrBot/data/plugin_data/{PLUGIN_NAME}/group_quota.sqlite"
DEFAULT_LIMIT = 40


def default_db_path() -> str:
    override = os.environ.get("NAI5_GROUP_QUOTA_PATH")
    if override:
        return override
    return DEFAULT_DB


class GroupQuotaStore:
    """Track successful NAI5 gens per group chat id."""

    def __init__(self, db_path: str | Path | None = None, limit: int = DEFAULT_LIMIT):
        self.path = Path(db_path or default_db_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit = max(0, int(limit))
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS group_nai5_quota (
                        group_id TEXT PRIMARY KEY,
                        used_count INTEGER NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL DEFAULT 0,
                        used_date TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                cols = {
                    r[1]
                    for r in conn.execute("PRAGMA table_info(group_nai5_quota)").fetchall()
                }
                if "used_date" not in cols:
                    conn.execute(
                        "ALTER TABLE group_nai5_quota ADD COLUMN used_date "
                        "TEXT NOT NULL DEFAULT ''"
                    )
                conn.commit()
            finally:
                conn.close()

    def set_limit(self, limit: int) -> None:
        self.limit = max(0, int(limit))

    @staticmethod
    def _today() -> str:
        from datetime import date

        return date.today().isoformat()

    def get_count(self, gid: str | int | None) -> int:
        key = str(gid or "").strip()
        if not key:
            return 0
        today = self._today()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT used_count, used_date FROM group_nai5_quota WHERE group_id = ?",
                    (key,),
                ).fetchone()
                if not row:
                    return 0
                used_date = ""
                try:
                    used_date = str(row["used_date"] or "")
                except (IndexError, KeyError):
                    used_date = ""
                if used_date and used_date != today:
                    conn.execute(
                        "UPDATE group_nai5_quota SET used_count = 0, used_date = ?, "
                        "updated_at = ? WHERE group_id = ?",
                        (today, time.time(), key),
                    )
                    conn.commit()
                    return 0
                return int(row["used_count"]) if row else 0
            finally:
                conn.close()

    def increment(self, gid: str | int | None, by: int = 1) -> int:
        key = str(gid or "").strip()
        if not key:
            return 0
        by = int(by)
        if by == 0:
            return self.get_count(key)
        with self._lock:
            conn = self._connect()
            try:
                now = time.time()
                today = self._today()
                row = conn.execute(
                    "SELECT used_count, used_date FROM group_nai5_quota WHERE group_id = ?",
                    (key,),
                ).fetchone()
                current = 0
                if row:
                    used_date = ""
                    try:
                        used_date = str(row["used_date"] or "")
                    except (IndexError, KeyError):
                        used_date = ""
                    if used_date == today:
                        current = int(row["used_count"] or 0)
                conn.execute(
                    """
                    INSERT INTO group_nai5_quota (group_id, used_count, updated_at, used_date)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET
                        used_count = excluded.used_count,
                        updated_at = excluded.updated_at,
                        used_date = excluded.used_date
                    """,
                    (key, current + by, now, today),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT used_count FROM group_nai5_quota WHERE group_id = ?",
                    (key,),
                ).fetchone()
                return int(row["used_count"]) if row else by
            finally:
                conn.close()

    def reset(self, gid: str | int | None) -> None:
        key = str(gid or "").strip()
        if not key:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO group_nai5_quota (group_id, used_count, updated_at, used_date)
                    VALUES (?, 0, ?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET
                        used_count = 0,
                        updated_at = excluded.updated_at,
                        used_date = excluded.used_date
                    """,
                    (key, time.time(), self._today()),
                )
                conn.commit()
            finally:
                conn.close()

    def remaining(self, gid: str | int | None, limit: int | None = None) -> int:
        lim = self.limit if limit is None else max(0, int(limit))
        used = self.get_count(gid)
        return max(0, lim - used)


# Module-level helpers for simple call sites (optional shared store).
_default_store: GroupQuotaStore | None = None
_default_lock = threading.Lock()


def get_store(db_path: str | Path | None = None, limit: int = DEFAULT_LIMIT) -> GroupQuotaStore:
    global _default_store
    path = str(db_path or default_db_path())
    with _default_lock:
        if (
            _default_store is None
            or str(_default_store.path) != path
            or _default_store.limit != max(0, int(limit))
        ):
            _default_store = GroupQuotaStore(path, limit=limit)
        return _default_store


def get_count(gid: str | int | None, store: GroupQuotaStore | None = None) -> int:
    return (store or get_store()).get_count(gid)


def increment(gid: str | int | None, store: GroupQuotaStore | None = None) -> int:
    return (store or get_store()).increment(gid)


def reset(gid: str | int | None, store: GroupQuotaStore | None = None) -> None:
    (store or get_store()).reset(gid)


def remaining(
    gid: str | int | None,
    limit: int | None = None,
    store: GroupQuotaStore | None = None,
) -> int:
    st = store or get_store()
    return st.remaining(gid, limit=limit)
