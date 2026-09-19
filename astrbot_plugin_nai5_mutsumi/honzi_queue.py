"""nai5本子 队列状态机（进程内，可单测）。

一会话同时只跑一本；取消只改标志，由 worker 在页间停下。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    PENDING = "pending"
    RESOLVING = "resolving"
    SENDING_ZIP = "sending_zip"
    FILTERING = "filtering"
    QUEUED = "queued"
    GENERATING = "generating"
    SENDING = "sending"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


_ACTIVE = {
    JobStatus.PENDING,
    JobStatus.RESOLVING,
    JobStatus.SENDING_ZIP,
    JobStatus.FILTERING,
    JobStatus.QUEUED,
    JobStatus.GENERATING,
    JobStatus.SENDING,
}

_TERMINAL = {JobStatus.DONE, JobStatus.CANCELLED, JobStatus.FAILED}


def session_key(user_id: str | int | None, group_id: str | int | None) -> str:
    uid = str(user_id or "").strip() or "?"
    gid = str(group_id or "").strip()
    return f"g:{gid}:{uid}" if gid else f"p:{uid}"


@dataclass
class HonziJob:
    job_id: str
    session_key: str
    user_id: str
    group_id: str
    requirement: str
    status: JobStatus = JobStatus.PENDING
    total_pages: int = 0
    current_page: int = 0
    kept: int = 0
    dropped: int = 0
    generated: int = 0
    zip_path: str = ""
    album_id: str = ""
    filter_backend: str = ""
    cancel_requested: bool = False
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def is_active(self) -> bool:
        return self.status in _ACTIVE and not (
            self.cancel_requested and self.status in _TERMINAL
        )

    def progress_text(self) -> str:
        req = (self.requirement or "原角").strip()
        if len(req) > 40:
            req = req[:40] + "…"
        album = self.album_id or "?"
        if self.status == JobStatus.CANCELLED or (
            self.cancel_requested and self.status in _ACTIVE
        ):
            extra = "（将在本页结束后停下）" if self.status in _ACTIVE else ""
            return f"……本子任务已取消{extra}。{album} {self.generated}/{self.total_pages or '?'}"
        if self.status == JobStatus.FAILED:
            return f"……本子失败。{self.error or '未知错误'}"
        if self.status == JobStatus.DONE:
            return (
                f"……本子跑完了。{album} 出图 {self.generated}，"
                f"保留 {self.kept}，剔除 {self.dropped}。"
            )
        stage = {
            JobStatus.PENDING: "排队",
            JobStatus.RESOLVING: "找本子包",
            JobStatus.SENDING_ZIP: "回传安装包",
            JobStatus.FILTERING: "本地滤页",
            JobStatus.QUEUED: "等待出图",
            JobStatus.GENERATING: "反推出图",
            JobStatus.SENDING: "发图",
        }.get(self.status, self.status.value)
        page = f"{self.current_page}/{self.total_pages}" if self.total_pages else "…"
        return (
            f"……本子进行中：{stage} {page}。"
            f"要求「{req}」。nai5本子取消 可停。"
        )


class HonziQueue:
    """进程内本子任务表。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, HonziJob] = {}
        self._by_session: dict[str, str] = {}

    def get(self, job_id: str) -> HonziJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def get_session(self, key: str) -> HonziJob | None:
        with self._lock:
            jid = self._by_session.get(key)
            return self._jobs.get(jid) if jid else None

    def active_for(self, key: str) -> HonziJob | None:
        job = self.get_session(key)
        if job and job.status in _ACTIVE:
            return job
        return None

    def create(
        self,
        *,
        user_id: str,
        group_id: str = "",
        requirement: str = "",
        replace_active: bool = False,
    ) -> HonziJob:
        key = session_key(user_id, group_id)
        with self._lock:
            existing = self.active_for(key)
            if existing and not replace_active:
                raise JobBusyError(existing)
            if existing and replace_active:
                existing.cancel_requested = True
                existing.status = JobStatus.CANCELLED
                existing.updated_at = time.time()
            job = HonziJob(
                job_id=uuid.uuid4().hex[:12],
                session_key=key,
                user_id=str(user_id or ""),
                group_id=str(group_id or ""),
                requirement=(requirement or "").strip(),
            )
            self._jobs[job.job_id] = job
            self._by_session[key] = job.job_id
            return job

    def cancel(self, key: str) -> HonziJob | None:
        with self._lock:
            job = self.active_for(key)
            if not job:
                return None
            job.cancel_requested = True
            if job.status in {JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RESOLVING}:
                job.status = JobStatus.CANCELLED
            job.updated_at = time.time()
            return job

    def should_stop(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return True
        return bool(job.cancel_requested) or job.status in {
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }

    def update(self, job_id: str, **fields: Any) -> HonziJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for k, v in fields.items():
                if k == "status" and isinstance(v, str):
                    v = JobStatus(v)
                if not hasattr(job, k):
                    raise AttributeError(k)
                setattr(job, k, v)
            job.updated_at = time.time()
            if job.cancel_requested and job.status in _ACTIVE:
                # 取消请求优先于继续推进（页间 worker 会读 should_stop）
                pass
            if job.cancel_requested and job.status in {
                JobStatus.PENDING,
                JobStatus.QUEUED,
            }:
                job.status = JobStatus.CANCELLED
            return job

    def mark_page(self, job_id: str, index: int, *, generated: bool = False) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.current_page = index
            if generated:
                job.generated += 1
            job.updated_at = time.time()

    def finish(self, job_id: str, *, error: str = "") -> HonziJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.cancel_requested:
                job.status = JobStatus.CANCELLED
            elif error:
                job.status = JobStatus.FAILED
                job.error = error
            else:
                job.status = JobStatus.DONE
            job.updated_at = time.time()
            return job


class JobBusyError(RuntimeError):
    def __init__(self, job: HonziJob):
        self.job = job
        super().__init__(f"session busy: {job.job_id}")


# 插件进程共享一份
_default: HonziQueue | None = None
_default_lock = threading.Lock()


def get_queue() -> HonziQueue:
    global _default
    with _default_lock:
        if _default is None:
            _default = HonziQueue()
        return _default
