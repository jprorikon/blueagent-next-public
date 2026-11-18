#!/usr/bin/env python3
"""心跳台詞不能唐突（owner 2026-10-24：「這次的搭話內容也有點唐突」）。

實測那句：`オーナー、その足跡、私も追いかけてたよ？　ねえ、なんで先に私のこと調べたの？`
——她拿 TRPG 播報裡的「足跡」和這串討論的片段去搭話，主人根本沒說過那些。

兩個原因：
  ① prompt 寫「前置きも説明も書かない」⇒ 只能從半句開始，聽起來沒頭沒腦
  ② 素材裡混了 TRPG 第三人稱敘事與客人那條線的預想

判據：
  A. prompt 一定帶「状況」（多久沒說話／在做什麼／主人最後說了什麼）
  B. prompt 一定要求自然入り方、且禁止拿主人沒說過的事當前提
  C. 素材裡的 TRPG 播報字眼不得進入「最近在做的事」
"""

from __future__ import annotations

import inspect
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.compose import bootstrap  # noqa: E402
from agent.brain.compose import heartbeat_compose  # noqa: E402  (心跳台詞的組裝從 build 拆出來了)
from agent.brain.compose.bootstrap import heartbeat_prompt  # noqa: E402


class TestHeartbeatPrompt(unittest.TestCase):
    def test_has_situation_and_natural_opening(self):
        body = heartbeat_prompt("（いまの気分：ごきげん）",
                                ["主人と最後に話してから約12分", "いまやっていたこと：規則書を読んでいた"],
                                ["最近在做的事：規則書を読んでいた"])
        self.assertIn("状況：", body)
        self.assertIn("自然な入り方", body)
        self.assertIn("主人が言っていないことを前提にしない", body)
        self.assertIn("12分", body)
        self.assertIn("規則書", body)

    def test_old_cold_open_instruction_is_gone(self):
        body = heartbeat_prompt("", [], [])
        self.assertNotIn("前置きも説明も書かない", body,
                         "舊指令會逼她從半句開始講")

    def test_empty_inputs_still_produce_a_prompt(self):
        body = heartbeat_prompt("", [], [])
        self.assertIn("一言だけ話しかけたい", body)


class TestHeartbeatSources(unittest.TestCase):
    def test_situation_uses_idle_and_last_owner_line(self):
        source = inspect.getsource(heartbeat_compose.make_heartbeat_composer)
        self.assertIn("主人と最後に話してから約", source)
        self.assertIn("主人が最後に言っていたこと", source)
        # 2026-11-11：退路（活線／raw）也一律只取主人的話（行為測試見 test_diagnosis_20261110.py）
        self.assertIn("owner_words(dialogue, principals)", source, "只取主人真的說過的話")

    def test_owner_words_are_looked_up_across_threads(self):
        """2026-11-08：最近活線可能是角色↔角色的線，主人的話要用跨線查的。"""
        source = inspect.getsource(heartbeat_compose.make_heartbeat_composer)
        self.assertIn("recent_owner_lines", source)
        self.assertIn("角色↔角色", source)

    def test_trpg_narration_is_filtered_from_grounds(self):
        source = inspect.getsource(heartbeat_compose.make_heartbeat_composer)
        self.assertIn('skip = ("足跡"', source)
        self.assertIn("any(mark in str(row) for mark in skip)", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
