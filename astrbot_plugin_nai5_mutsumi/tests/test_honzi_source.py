"""JM zip 解析 / 选取：不写死单一本子。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from honzi_source import (  # noqa: E402
    SessionZipIndex,
    SessionZipRecord,
    ZipCandidate,
    ZipHint,
    extract_zip_pages,
    parse_album_ids,
    parse_zip_hints,
    pick_best_zip,
    resolve_zip,
)


class TestParseAlbum(unittest.TestCase):
    def test_explicit_jm(self):
        self.assertEqual(parse_album_ids("先 jm 422866 再反推"), ["422866"])
        self.assertEqual(parse_album_ids("jmi 10010"), ["10010"])

    def test_brackets_and_filename(self):
        ids = parse_album_ids("【998877】title_dickding.zip")
        self.assertIn("998877", ids)

    def test_no_hardcoded_sample(self):
        a = parse_album_ids("jm 11111")
        b = parse_album_ids("jm 22222")
        self.assertEqual(a, ["11111"])
        self.assertEqual(b, ["22222"])
        self.assertNotEqual(a, b)

    def test_hints_from_reply_and_file(self):
        h = parse_zip_hints(
            "下载完成 本子 55555 密码 dickding",
            extra_paths=["/tmp/55555_dickding.zip"],
        )
        self.assertIn("55555", h.album_ids)
        self.assertTrue(any("55555" in p for p in h.paths))


class TestPickZip(unittest.TestCase):
    def test_prefers_matching_id_not_newest_other(self):
        older = ZipCandidate(Path("/x/11111_dickding.zip"), mtime=1, album_id="11111")
        newer = ZipCandidate(Path("/x/99999_dickding.zip"), mtime=9, album_id="99999")
        hint = ZipHint(album_ids=("11111",))
        got = pick_best_zip([newer, older], hint)
        self.assertEqual(got.album_id, "11111")

    def test_session_last_when_no_hint(self):
        last = SessionZipRecord(
            session_key="p:1",
            user_id="1",
            path="",  # 文件不存在则不会短路
            album_id="33333",
            mtime=1,
        )
        c = ZipCandidate(Path("/x/33333.zip"), mtime=2, album_id="33333")
        got = pick_best_zip([c], ZipHint(), last=last)
        self.assertEqual(got.album_id, "33333")

    def test_resolve_from_real_dir(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            p1 = d / "10001_dickding.zip"
            p2 = d / "20002_dickding.zip"
            p1.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            time.sleep(0.02)
            p2.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            got = resolve_zip(ZipHint(album_ids=("10001",)), search_dirs=[d])
            self.assertIsNotNone(got)
            self.assertEqual(got.album_id, "10001")
            newest = resolve_zip(ZipHint(), search_dirs=[d])
            self.assertEqual(newest.album_id, "20002")


class TestExtractAndIndex(unittest.TestCase):
    def test_extract_pages_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / "30003_dickding.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                # 最小 JPEG (1x1) 太小会被跳过；写大一点
                blob = b"\xff\xd8\xff\xe0" + (b"\x00" * 9000) + b"\xff\xd9"
                zf.writestr("pages/2.jpg", blob)
                zf.writestr("pages/10.jpg", blob)
                zf.writestr("pages/1.jpg", blob)
            work, pages = extract_zip_pages(zpath, Path(td) / "out", password="")
            names = [p.name for p in pages]
            self.assertEqual(names, ["1.jpg", "2.jpg", "10.jpg"])
            self.assertTrue(work.is_dir())

    def test_session_index_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            idx = SessionZipIndex(Path(td) / "last.json")
            rec = SessionZipRecord(
                session_key="g:1:2",
                user_id="2",
                path="/data/40004.zip",
                album_id="40004",
                mtime=123.0,
            )
            idx.put(rec)
            self.assertEqual(idx.get("g:1:2").album_id, "40004")
            self.assertEqual(idx.get_user("2").path, "/data/40004.zip")


if __name__ == "__main__":
    unittest.main()
