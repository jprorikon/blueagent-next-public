#!/usr/bin/env python3
"""「問句／指令」分不清的回歸（owner 2026-10-24）。

實測踩到：オーナー 說「役割カードの話忘れて」（＝那件事忘掉吧），她回
「忘れちゃったの？…忘れたふりしてあげる」——把**指令**讀成**對方在陳述自己忘了什麼**。

判據：
  ① utterance_kind 認得粗俗但常見的依賴／命令形，也認得問句；認不出回空字不硬猜
  ② 「〜してくれる？」這種兩者都像的，判成問句（在問，不是在命令）
  ③ 主 prompt 一定會帶 [[utterance]] 讀法區塊（判不出來也要給通用讀法）
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.messages import build_messages          # noqa: E402
from agent.brain.ports import BrainRequest                      # noqa: E402
from agent.brain.domain.social_condition import (              # noqa: E402
    QUESTION, REQUEST, STATEMENT, utterance_kind)


class TestUtteranceKind(unittest.TestCase):
    def test_imperatives(self):
        for text in ("役割カードの話忘れて", "もうやめて", "これ見て", "幫我把那個改成紅色",
                     "記得幫我買", "今晚一起准备角色卡吧"):
            self.assertEqual(utterance_kind(text), REQUEST, text)

    def test_questions(self):
        for text in ("忘れちゃったの？", "これ何？", "明日の予定は？", "你吃了嗎"):
            self.assertEqual(utterance_kind(text), QUESTION, text)

    def test_statements(self):
        self.assertEqual(utterance_kind("今日はちょっと疲れたよ。"), STATEMENT)

    def test_ambiguous_gives_no_guess(self):
        self.assertEqual(utterance_kind("役割カード"), "")

    def test_offer_question_counts_as_question(self):
        """「〜してくれる？」是在問，不是在命令。"""
        self.assertEqual(utterance_kind("これ消してくれる？"), QUESTION)


class TestUtteranceBlock(unittest.TestCase):
    def body(self, text: str) -> str:
        request = BrainRequest(thread_id="t", entity="mira", principal="owner",
                               text=text, system="あなたはミラです。", meta={})
        messages = build_messages(request, max_history=0, max_chars=4000)
        return "\n".join(str(item.get("content") or "") for item in messages)

    def test_request_block_is_labelled(self):
        body = self.body("役割カードの話忘れて")
        self.assertIn("[[utterance]]", body)
        self.assertIn("依頼・指示", body)
        self.assertIn("質問に言い換えない", body)

    def test_unknown_text_still_gets_generic_reading_rule(self):
        body = self.body("役割カード")
        self.assertIn("[[utterance]]", body)
        self.assertIn("見分ける", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
