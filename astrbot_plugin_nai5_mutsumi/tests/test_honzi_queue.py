"""本子队列状态机。"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from honzi_queue import (  # noqa: E402
    HonziQueue,
    JobBusyError,
    JobStatus,
    session_key,
)


class TestHonziQueue(unittest.TestCase):
    def setUp(self) -> None:
        self.q = HonziQueue()

    def test_session_key(self):
        self.assertEqual(session_key("1", "99"), "g:99:1")
        self.assertEqual(session_key("1", ""), "p:1")

    def test_create_and_progress(self):
        job = self.q.create(user_id="u1", group_id="g1", requirement="换成博士")
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertTrue(job.is_active())
        self.q.update(job.job_id, status=JobStatus.FILTERING, total_pages=4, kept=3, dropped=1)
        self.q.update(job.job_id, status=JobStatus.GENERATING)
        self.q.mark_page(job.job_id, 1, generated=True)
        self.q.mark_page(job.job_id, 2, generated=True)
        got = self.q.get(job.job_id)
        self.assertEqual(got.generated, 2)
        self.assertEqual(got.current_page, 2)
        self.assertIn("反推出图", got.progress_text())
        self.q.finish(job.job_id)
        self.assertEqual(self.q.get(job.job_id).status, JobStatus.DONE)
        self.assertIn("跑完了", self.q.get(job.job_id).progress_text())

    def test_one_active_per_session(self):
        self.q.create(user_id="u1", group_id="g1", requirement="a")
        with self.assertRaises(JobBusyError):
            self.q.create(user_id="u1", group_id="g1", requirement="b")
        # 不同会话互不挡
        other = self.q.create(user_id="u2", group_id="g1", requirement="c")
        self.assertTrue(other.is_active())

    def test_cancel_stops_worker(self):
        job = self.q.create(user_id="u1", requirement="换成特蕾西娅")
        self.q.update(job.job_id, status=JobStatus.GENERATING, total_pages=10)
        self.assertFalse(self.q.should_stop(job.job_id))
        cancelled = self.q.cancel(job.session_key)
        self.assertIsNotNone(cancelled)
        self.assertTrue(cancelled.cancel_requested)
        self.assertTrue(self.q.should_stop(job.job_id))
        self.q.finish(job.job_id)
        self.assertEqual(self.q.get(job.job_id).status, JobStatus.CANCELLED)

    def test_cancel_missing(self):
        self.assertIsNone(self.q.cancel("p:nobody"))

    def test_replace_active(self):
        a = self.q.create(user_id="u1", requirement="a")
        b = self.q.create(user_id="u1", requirement="b", replace_active=True)
        self.assertEqual(self.q.get(a.job_id).status, JobStatus.CANCELLED)
        self.assertEqual(self.q.active_for(b.session_key).job_id, b.job_id)

    def test_fail(self):
        job = self.q.create(user_id="u1", requirement="x")
        self.q.finish(job.job_id, error="no zip")
        self.assertEqual(self.q.get(job.job_id).status, JobStatus.FAILED)
        self.assertIn("no zip", self.q.get(job.job_id).progress_text())


if __name__ == "__main__":
    unittest.main()
