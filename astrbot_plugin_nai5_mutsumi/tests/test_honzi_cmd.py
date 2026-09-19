"""nai5本子 指令解析。"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from honzi_cmd import parse_honzi_command, is_honzi_text  # noqa: E402


class TestHonziCmd(unittest.TestCase):
    def test_start_with_requirement(self):
        c = parse_honzi_command("nai5本子 角色替换为博士和特蕾西娅")
        self.assertEqual(c.kind, "start")
        self.assertEqual(c.requirement, "角色替换为博士和特蕾西娅")
        self.assertTrue(c.is_honzi)

    def test_start_empty_keeps_original_cast(self):
        c = parse_honzi_command("nai5本子")
        self.assertEqual(c.kind, "start")
        self.assertEqual(c.requirement, "")

    def test_aliases_and_at(self):
        for raw in (
            "@莫提斯 nai5本子 换成能天使",
            "/nai5本子：换成博士",
            "睦画本子 改成阿米娅",
            "nai5画本子 角色替换为博士和特蕾西娅",
        ):
            c = parse_honzi_command(raw)
            self.assertEqual(c.kind, "start", raw)
            self.assertTrue(c.requirement)

    def test_cancel_variants(self):
        for raw in (
            "nai5本子取消",
            "nai5本子 取消",
            "nai5本子停止",
            "/nai5本子 cancel",
            "睦画本子 停止",
        ):
            c = parse_honzi_command(raw)
            self.assertEqual(c.kind, "cancel", raw)

    def test_status_variants(self):
        for raw in ("nai5本子进度", "nai5本子 状态", "nai5本子 status"):
            c = parse_honzi_command(raw)
            self.assertEqual(c.kind, "status", raw)

    def test_not_honzi(self):
        for raw in ("nai5 窗边侧光", "nai5反推漫画 换成祥子", "jm 12576", ""):
            c = parse_honzi_command(raw)
            self.assertEqual(c.kind, "unknown", raw)
            self.assertFalse(is_honzi_text(raw))


if __name__ == "__main__":
    unittest.main()
