"""纯函数测试：坐标映射、版式分类、弱分镜、对白校验、LAYOUT 应用。"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from manga_layout import (  # noqa: E402
    MangaLayout,
    Panel,
    LayoutSlot,
    apply_layout_to_chars,
    assign_positions_from_slots,
    center_to_pos,
    classify_layout_kind,
    extract_n_panel,
    geometry_sentence,
    is_action_summary_text,
    layout_is_weak,
    looks_like_letter_as_row,
    merge_prompt_with_layout,
    merge_slot_dialogue,
    parse_layout_block,
    pos_to_center,
    sanitize_chars_dialogue,
    transpose_letter_row_positions,
)


class TestCoordRoundtrip(unittest.TestCase):
    def test_official_grid_centers(self):
        self.assertEqual(pos_to_center("A1"), (0.1, 0.1))
        self.assertEqual(pos_to_center("C3"), (0.5, 0.5))
        self.assertEqual(pos_to_center("E5"), (0.9, 0.9))
        self.assertEqual(pos_to_center("B2"), (0.3, 0.3))

    def test_center_to_pos_inverse(self):
        for letter in "ABCDE":
            for digit in "12345":
                pos = f"{letter}{digit}"
                self.assertEqual(center_to_pos(*pos_to_center(pos)), pos)

    def test_strip_centers_map_to_same_column(self):
        """五条全宽横条的中心 → C1…C5，而不是 A3…E3。"""
        ys = [0.10, 0.30, 0.50, 0.70, 0.90]
        got = [center_to_pos(0.5, y) for y in ys]
        self.assertEqual(got, ["C1", "C2", "C3", "C4", "C5"])


class TestLayoutKind(unittest.TestCase):
    def test_stacked_strips(self):
        panels = [
            Panel(id=i + 1, x=0.0, y=i * 0.2, w=1.0, h=0.2, shot="close-up")
            for i in range(5)
        ]
        self.assertEqual(classify_layout_kind(panels), "stacked_strips")
        lay = MangaLayout(panel_count=5, kind="stacked_strips", panels=panels)
        self.assertIn("full-width horizontal strips", geometry_sentence(lay))

    def test_stacked_columns(self):
        panels = [
            Panel(id=1, x=0.0, y=0.0, w=0.33, h=1.0),
            Panel(id=2, x=0.33, y=0.0, w=0.34, h=1.0),
            Panel(id=3, x=0.67, y=0.0, w=0.33, h=1.0),
        ]
        self.assertEqual(classify_layout_kind(panels), "stacked_columns")

    def test_grid_2x2(self):
        panels = [
            Panel(id=1, x=0.0, y=0.0, w=0.5, h=0.5),
            Panel(id=2, x=0.5, y=0.0, w=0.5, h=0.5),
            Panel(id=3, x=0.0, y=0.5, w=0.5, h=0.5),
            Panel(id=4, x=0.5, y=0.5, w=0.5, h=0.5),
        ]
        self.assertEqual(classify_layout_kind(panels), "grid")

    def test_asymmetric_l_shape(self):
        panels = [
            Panel(id=1, x=0.4, y=0.0, w=0.6, h=1.0),
            Panel(id=2, x=0.0, y=0.0, w=0.4, h=0.45),
            Panel(id=3, x=0.0, y=0.5, w=0.4, h=0.5),
        ]
        self.assertEqual(classify_layout_kind(panels), "asymmetric")


class TestAssignPositions(unittest.TestCase):
    def test_five_strips_single_occupant(self):
        slots = [
            LayoutSlot(panel=i + 1, cx=0.5, cy=0.1 + i * 0.2) for i in range(5)
        ]
        self.assertEqual(
            assign_positions_from_slots(slots),
            ["C1", "C2", "C3", "C4", "C5"],
        )

    def test_same_panel_two_people_spread_letters(self):
        slots = [
            LayoutSlot(panel=2, cx=0.35, cy=0.30),
            LayoutSlot(panel=2, cx=0.65, cy=0.30),
        ]
        self.assertEqual(assign_positions_from_slots(slots), ["B2", "D2"])

    def test_asymmetric_uses_bbox_centers(self):
        slots = [
            LayoutSlot(panel=1, cx=0.75, cy=0.45),  # right tall
            LayoutSlot(panel=2, cx=0.20, cy=0.20),  # left top
            LayoutSlot(panel=3, cx=0.20, cy=0.80),  # left bottom
        ]
        got = assign_positions_from_slots(slots)
        self.assertEqual(got[0][0], "D")  # right-ish
        self.assertIn(got[1][1], "12")
        self.assertIn(got[2][1], "45")


class TestParseAndApply(unittest.TestCase):
    _JSON = """
LAYOUT:
{"panel_count":5,"reading":"rtl","kind":"stacked_strips","panels":[
 {"id":1,"x":0,"y":0.00,"w":1,"h":0.20,"shot":"upper body"},
 {"id":2,"x":0,"y":0.20,"w":1,"h":0.16,"shot":"hands"},
 {"id":3,"x":0,"y":0.36,"w":1,"h":0.20,"shot":"close-up"},
 {"id":4,"x":0,"y":0.56,"w":1,"h":0.16,"shot":"hands"},
 {"id":5,"x":0,"y":0.72,"w":1,"h":0.28,"shot":"close-up"}
],"slots":[
 {"panel":1,"cx":0.5,"cy":0.10,"text":"","kind":""},
 {"panel":2,"cx":0.32,"cy":0.28,"text":"直到遇见了一个……","kind":"bubble"},
 {"panel":2,"cx":0.68,"cy":0.28,"text":"","kind":""},
 {"panel":3,"cx":0.5,"cy":0.46,"text":"连星星都看不清楚的人","kind":"bubble"},
 {"panel":4,"cx":0.35,"cy":0.64,"text":"","kind":""},
 {"panel":5,"cx":0.5,"cy":0.86,"text":"","kind":""}
]}
"""

    def test_parse_layout_and_override_wrong_letters(self):
        lay = parse_layout_block(self._JSON)
        self.assertIsNotNone(lay)
        assert lay is not None
        self.assertEqual(lay.panel_count, 5)
        self.assertEqual(lay.kind, "stacked_strips")
        # 日志里的错误 position：A3 B2 B4 C3 D3 E3
        chars = [
            {"prompt": "doctor (arknights), boy", "position": "A3"},
            {"prompt": 'speech bubble, text: "闭眼", doctor (arknights)', "position": "B2"},
            {"prompt": "theresa (arknights), girl", "position": "B4"},
            {"prompt": "theresa (arknights), girl", "position": "C3"},
            {"prompt": "doctor (arknights), boy", "position": "D3"},
            {"prompt": "theresa (arknights), girl", "position": "E3"},
        ]
        # 6 槽 vs 5 LAYOUT slots：前 5 用几何，多出的按格补
        out = apply_layout_to_chars(chars, lay)
        pos = [c["position"] for c in out]
        # 不应再是一条水平线（全是 digit=3 或 letter 当行）
        digits = {p[1] for p in pos}
        self.assertGreaterEqual(len(digits), 3)
        self.assertIn("1", "".join(pos))  # 有顶格
        self.assertTrue(any(p[1] in "45" for p in pos))  # 有底格

    def test_wrong_kind_label_overridden_by_geometry(self):
        raw = """LAYOUT:
{"panel_count":5,"kind":"asymmetric","panels":[
 {"id":1,"x":0,"y":0.0,"w":1,"h":0.2},
 {"id":2,"x":0,"y":0.2,"w":1,"h":0.2},
 {"id":3,"x":0,"y":0.4,"w":1,"h":0.2},
 {"id":4,"x":0,"y":0.6,"w":1,"h":0.2},
 {"id":5,"x":0,"y":0.8,"w":1,"h":0.2}
]}"""
        lay = parse_layout_block(raw)
        self.assertIsNotNone(lay)
        assert lay is not None
        self.assertEqual(lay.kind, "stacked_strips")

    def test_trailing_comma_json(self):
        raw = 'LAYOUT: {"panel_count":2,"panels":[{"id":1,"x":0,"y":0,"w":1,"h":0.5,},{"id":2,"x":0,"y":0.5,"w":1,"h":0.5,}],}'
        lay = parse_layout_block(raw)
        self.assertIsNotNone(lay)


class TestDialogue(unittest.TestCase):
    def test_action_summary_rejected(self):
        self.assertTrue(is_action_summary_text("闭眼"))
        self.assertTrue(is_action_summary_text("eyes closed"))
        self.assertTrue(is_action_summary_text("伸手"))
        self.assertTrue(is_action_summary_text("sitting"))
        self.assertFalse(is_action_summary_text("直到遇见了一个人"))
        self.assertFalse(is_action_summary_text("连星星都看不清楚的人"))
        self.assertFalse(is_action_summary_text("星？为什么"))

    def test_merge_replaces_bad_text_with_layout_original(self):
        body = 'theresa (arknights), 1.3::eyes closed::, speech bubble, text: "闭眼"'
        slot = LayoutSlot(panel=3, cx=0.5, cy=0.5, text="连星星都看不清楚的人", kind="bubble")
        out = merge_slot_dialogue(body, slot)
        self.assertIn('text: "连星星都看不清楚的人"', out)
        self.assertNotIn("闭眼", out)

    def test_sanitize_drops_action_text(self):
        chars = [{"prompt": 'doctor, speech bubble, text: "闭眼"', "position": "C3"}]
        out = sanitize_chars_dialogue(chars)
        self.assertNotIn("闭眼", out[0]["prompt"])


class TestPromptMerge(unittest.TestCase):
    def test_replaces_comic_page_with_n_panel(self):
        lay = MangaLayout(
            panel_count=5,
            kind="stacked_strips",
            reading="rtl",
            panels=[
                Panel(id=i + 1, x=0.0, y=i * 0.2, w=1.0, h=0.2) for i in range(5)
            ],
        )
        prompt = "1.40::comic page::, 1.35::asymmetric multi-panel layout::, speech bubble, nsfw, best quality"
        out = merge_prompt_with_layout(prompt, lay)
        self.assertIn("5-panel manga page", out)
        self.assertNotIn("comic page", out)
        self.assertNotIn("speech bubble", out)
        self.assertIn("right-to-left", out)
        self.assertIn("full-width horizontal strips", out)

    def test_strips_story_leakage(self):
        lay = MangaLayout(
            panel_count=3,
            kind="stacked_strips",
            panels=[Panel(id=i + 1, x=0, y=i * 0.33, w=1, h=0.33) for i in range(3)],
        )
        prompt = "3-panel manga page, top panel: Character 1 looking away and holding sphere, soft light"
        out = merge_prompt_with_layout(prompt, lay)
        self.assertNotIn("looking away", out)
        self.assertIn("3-panel manga page", out)

    def test_extract_n_panel(self):
        self.assertEqual(extract_n_panel("1.40::5-panel manga page::, foo"), 5)
        self.assertIsNone(extract_n_panel("comic page"))


class TestWeakLayout(unittest.TestCase):
    def test_missing_layout_and_npanel_is_weak(self):
        self.assertTrue(layout_is_weak(None, "comic page", [{"prompt": "a"}]))
        self.assertTrue(
            layout_is_weak(None, "1.40::5-panel manga page::", [{}, {}, {}])
        )

    def test_good_layout_not_weak(self):
        lay = parse_layout_block(
            'LAYOUT:{"panel_count":3,"panels":['
            '{"id":1,"x":0,"y":0,"w":1,"h":0.33},'
            '{"id":2,"x":0,"y":0.33,"w":1,"h":0.33},'
            '{"id":3,"x":0,"y":0.66,"w":1,"h":0.34}]}'
        )
        self.assertFalse(
            layout_is_weak(lay, "1.40::3-panel manga page::", [{}, {}, {}])
        )


class TestFallbackTranspose(unittest.TestCase):
    def test_log_case_a3_e3_detected(self):
        pos = ["A3", "B2", "B4", "C3", "D3", "E3"]
        self.assertTrue(looks_like_letter_as_row(pos))

    def test_true_vertical_c1_c5_not_swapped(self):
        self.assertFalse(looks_like_letter_as_row(["C1", "C2", "C3", "C4", "C5"]))

    def test_grid_not_swapped(self):
        self.assertFalse(looks_like_letter_as_row(["B2", "D2", "B4", "D4"]))

    def test_three_across_not_swapped(self):
        # 真·一行三人：不要当竖条转置
        self.assertFalse(looks_like_letter_as_row(["A3", "C3", "E3"]))

    def test_transpose_a3_to_c1(self):
        chars = [
            {"prompt": "a", "position": "A3"},
            {"prompt": "b", "position": "E3"},
        ]
        out = transpose_letter_row_positions(chars)
        self.assertEqual(out[0]["position"], "C1")
        self.assertEqual(out[1]["position"], "C5")


if __name__ == "__main__":
    unittest.main()
