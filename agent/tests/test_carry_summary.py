#!/usr/bin/env python3
"""承接段用「提煉窗口摘要」（owner 2026-10-23：換段要接得上，且要是壓縮過的）。

判據：
  ① 有窗口摘要 ⇒ `thread.carry_summary` 用它（不是把前幾句原話串起來）；
  ② 沒有窗口摘要 ⇒ 退回逐句摘要（行為不變）；
  ③ 窗口摘要查詢失敗 ⇒ 一樣退回，封片不該因此失敗；
  ④ 換世代時承接摘要會被繼承（下一段仍接得上）。
"""

from __future__ import annotations

import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.episode import CutReason
from agent.brain.domain.thread import ThreadKey, TurnRecord
from agent.brain.runtime.episodes import EpisodeArchive
from agent.brain.runtime.threadstore import ThreadStore


def make_thread(turns: int = 3):
    key = ThreadKey(scope="owner", entity="mira", scene="chat", purpose="talk")
    store = ThreadStore()
    from agent.brain.domain.thread import SourceClass
    attach = store.attach(key, now=1000.0, window=600.0, lane=SourceClass.INTERACTIVE)
    thread = attach.thread
    for index in range(turns):
        thread.record(TurnRecord(turn_id=f"t{index}", role="user" if index % 2 == 0 else "assistant",
                                 text=f"第 {index} 句，講到小說與角色卡。", source="app",
                                 at=1000.0 + index))
    return thread


class TestCarrySummary(unittest.TestCase):
    def test_window_summary_wins(self):
        window = lambda key, start, end: "在讀的小說：主人提到讀到第六卷，ミラ說也想讀。"
        archive = EpisodeArchive(summarize=lambda ep: "逐句：主人說…", window_summary=window)
        thread = make_thread()
        episode = archive.seal(thread, now=1010.0, reason=CutReason.IDLE)
        self.assertIsNotNone(episode)
        self.assertIn("第六卷", thread.carry_summary)
        self.assertIn("第六卷", episode.summary)

    def test_falls_back_to_turn_summary(self):
        archive = EpisodeArchive(summarize=lambda ep: "逐句：主人說…",
                                 window_summary=lambda key, start, end: "")
        thread = make_thread()
        archive.seal(thread, now=1010.0, reason=CutReason.IDLE)
        # 2026-10-25：承接段是壓縮產物 ⇒ 第一行帶門牌（`raw:<entity>#t=<起>+<n>`），
        # 正文照舊。要驗的是「摘要在、且指得回原文」，不是「字串一模一樣」。
        self.assertTrue(thread.carry_summary.startswith("〔raw:mira#t=t0+3〕"),
                        thread.carry_summary)
        self.assertIn("逐句：主人說…", thread.carry_summary)

    def test_carry_has_no_pointer_without_turn_ids(self):
        """拿不到 turn id ⇒ **不寫半個門牌**（錯的門牌比沒有更壞）。"""
        key = ThreadKey(scope="owner", entity="mira", scene="chat", purpose="talk")
        from agent.brain.domain.thread import SourceClass
        thread = ThreadStore().attach(key, now=1000.0, window=600.0,
                                      lane=SourceClass.INTERACTIVE).thread
        thread.record(TurnRecord(turn_id="", role="user", text="沒有 id 的一輪",
                                 source="app", at=1000.0))
        archive = EpisodeArchive(summarize=lambda ep: "逐句：沒有 id",
                                 window_summary=lambda key, start, end: "")
        archive.seal(thread, now=1010.0, reason=CutReason.IDLE)
        self.assertNotIn("〔raw:", thread.carry_summary)
        self.assertIn("逐句：沒有 id", thread.carry_summary)

    def test_broken_window_lookup_falls_back(self):
        def boom(key, start, end):
            raise RuntimeError("db gone")

        archive = EpisodeArchive(summarize=lambda ep: "逐句：還在", window_summary=boom)
        thread = make_thread()
        episode = archive.seal(thread, now=1010.0, reason=CutReason.IDLE)
        self.assertIsNotNone(episode, "窗口摘要壞了不該讓封片失敗")
        self.assertIn("逐句：還在", thread.carry_summary)
        self.assertTrue(thread.carry_summary.startswith("〔raw:mira#t=t0+3〕"),
                        thread.carry_summary)

    def test_new_generation_inherits_carry(self):
        key = ThreadKey(scope="owner", entity="mira", scene="chat", purpose="talk")
        store = ThreadStore()
        from agent.brain.domain.thread import SourceClass
        first = store.attach(key, now=1000.0, window=0.0, lane=SourceClass.INTERACTIVE).thread
        first.carry_summary = "在讀的小說：讀到第六卷。"
        store.persist()
        second = store.attach(key, now=5000.0, window=0.0, lane=SourceClass.INTERACTIVE).thread
        self.assertNotEqual(first.id, second.id)
        self.assertIn("第六卷", second.carry_summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
