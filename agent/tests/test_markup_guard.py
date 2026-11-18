#!/usr/bin/env python3
"""文字形式的工具呼叫（owner 2026-11-03 實機 bug：她回的不是話，是 `<tool_call>` 標籤）。

重現的事故原話：主人在 App 問ノリ一句話，端末上出現的是
`<tool_call><function=motion><parameter=name>nod</parameter></tool_call>`
——模型把工具呼叫寫成文字吐在正文裡，串流當成「她說的話」送了出去。

判據：
  ① 整段只有記法 ⇒ 正文為空、轉成一次真的工具呼叫（動作照樣做）；
  ② 記法夾在話中間 ⇒ 只吃掉記法，人話照說；
  ③ 串流被切斷、沒有收尾標籤 ⇒ 殘骸一樣吃掉，不流到端末；
  ④ 標籤被切成好幾塊送來（`<tool_` + `call>`）也要接得住。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.markup_guard import MarkupGuard, parse_markup, strip_markup  # noqa: E402

NOD = "<tool_call>\n<function=motion>\n<parameter=name>nod</parameter>\n</function>\n</tool_call>"


class TestMarkupGuard(unittest.TestCase):
    def test_pure_tool_call_never_reaches_text(self):
        guard = MarkupGuard()
        spoken = "".join(guard.feed(part) for part in NOD) + guard.flush()
        self.assertEqual(spoken.strip(), "")
        self.assertEqual([call["name"] for call in guard.swallowed], ["motion"])
        self.assertEqual(guard.swallowed[0]["arguments"], {"name": "nod"})

    def test_text_with_embedded_call(self):
        guard = MarkupGuard()
        spoken = guard.feed("うん、" + NOD + "わかった。") + guard.flush()
        self.assertEqual(spoken, "うん、わかった。")
        self.assertEqual(len(guard.swallowed), 1)

    def test_split_tag_across_chunks(self):
        guard = MarkupGuard()
        parts = ["<tool_", "call>\n<function=motion>\n<parameter=name>", "nod", "</parameter>",
                 "</function></tool_call>"]
        spoken = "".join(guard.feed(part) for part in parts) + guard.flush()
        self.assertEqual(spoken.strip(), "")
        self.assertEqual([c["name"] for c in guard.swallowed], ["motion"])

    def test_truncated_markup_is_swallowed(self):
        guard = MarkupGuard()
        spoken = guard.feed("<tool_call>\n<function=motion>\n<parameter=name>no") + guard.flush()
        self.assertEqual(spoken.strip(), "")

    def test_normal_reply_untouched(self):
        guard = MarkupGuard()
        text = "ノリだよ。今日はゲームのテンポを見てた。"
        self.assertEqual(guard.feed(text) + guard.flush(), text)

    def test_angle_bracket_words_survive(self):
        guard = MarkupGuard()
        text = "3 < 5 だよね、ふつうに小なり記号。"
        self.assertEqual(guard.feed(text) + guard.flush(), text)

    def test_strip_and_parse_helpers(self):
        self.assertEqual(strip_markup("あ" + NOD + "い"), "あい")
        self.assertEqual(parse_markup(NOD), {"name": "motion", "arguments": {"name": "nod"}})
        self.assertIsNone(parse_markup("ふつうの文"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
