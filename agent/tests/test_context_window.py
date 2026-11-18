#!/usr/bin/env python3
"""上下文窗口：固定段／承接段／滾動段／本輪段（owner 2026-10-22，預算放寬到 36K）。

判據：
  ① 承接段：`carry_summary` 會進 prompt（`[[before]]`），而且**換世代／封片後還在**；
  ② 滾動段：不再固定 8 條——預算內盡量多放；預算小的時候只放最近的；
  ③ 單條超長會截斷（不能一句話吃掉整個窗口）；
  ④ 砍在最舊一條 assistant 上時丟掉孤立回覆（不能只有答案沒有問題）；
  ⑤ 總長不超過 `max_chars`。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.messages import build_messages
from agent.brain.domain.episode import CutReason
from agent.brain.domain.event import SourceClass
from agent.brain.domain.ids import ThreadKey
from agent.brain.domain.thread import TurnRecord
from agent.brain.ports import BrainRequest
from agent.brain.runtime.episodes import EpisodeArchive
from agent.brain.runtime.threadstore import ThreadStore


def request(transcript, *, carry="", text="新しい質問", max_chars=36000, meta=None) -> BrainRequest:
    extra = {"transcript": list(transcript), "carry_summary": carry, "speak_lang": "ja"}
    extra.update(meta or {})
    return BrainRequest(
        thread_id="th_x", entity="mira", principal="owner", text=text,
        system="persona", meta=extra,
    )


def rows(count: int, chars: int = 20) -> list[dict]:
    out = []
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        out.append({"role": role, "text": f"{index:03d}-" + "あ" * chars})
    return out


class TestCarrySummary(unittest.TestCase):
    def test_carry_block_after_history_before_current(self):
        """承接段要留在歷史**之後**、這句話之前（ADR-0020：它在歷史之前會把快取打掉）。

        2026-11-02 起本輪段併進最後那條用戶訊息 ⇒ 承接段在最後一條裡、排在這句話前面。
        """
        messages = build_messages(request(rows(2), carry="之前聊到小説の話をしていた"))
        contents = [item["content"] for item in messages]
        carry_at = next(index for index, text in enumerate(contents) if "[[before]]" in text)
        history_at = next(index for index, text in enumerate(contents) if "000-" in text)
        last_at = len(contents) - 1
        self.assertGreater(carry_at, history_at, "承接段要在滾動段之後")
        self.assertEqual(carry_at, last_at, "承接段併在這一輪的用戶訊息裡")
        last = contents[last_at]
        self.assertLess(last.index("[[before]]"), last.index("新しい質問"), "承接段要在這句話之前")
        self.assertIn("小説", last)

    def test_turn_blocks_never_sit_before_history(self):
        """每回合都變的段（記憶／時間感）不准出現在歷史之前——那是快取失效的來源。"""
        req = request(rows(4), meta={"recall": ["主人は小説を読んでいる"],
                                     "clock_block": "いま 12:00、3 往復目"})
        contents = [item["content"] for item in build_messages(req)]
        history_at = next(index for index, text in enumerate(contents) if "000-" in text)
        for marker in ("[[memory]]", "[[clock]]"):
            where = next(index for index, text in enumerate(contents) if marker in text)
            self.assertGreater(where, history_at, f"{marker} 要在歷史之後")

    def test_no_carry_means_no_block(self):
        messages = build_messages(request(rows(2)))
        self.assertFalse(any("[[before]]" in str(item["content"]) for item in messages))

    def test_seal_writes_summary_back_to_thread(self):
        store = ThreadStore(path=None)
        key = ThreadKey(scope="owner", entity="mira", scene="dm", purpose="chat")
        thread = store.attach(key, now=1000.0, window=600.0,
                              lane=SourceClass.INTERACTIVE).thread
        thread.record(TurnRecord(turn_id="t1", role="user", text="小説の話", source="t", at=1000.0))
        thread.record(TurnRecord(turn_id="t2", role="assistant", text="佐々木とピーちゃん",
                                 source="t", at=1001.0))
        archive = EpisodeArchive(summarize=lambda _ep: "主人は『佐々木とピーちゃん』を読んでいる")
        self.assertIsNotNone(archive.seal(thread, 1002.0, CutReason.TURNS))
        self.assertIn("佐々木", thread.carry_summary, "封片後摘要要留在線上")

    def test_new_generation_inherits_carry(self):
        store = ThreadStore(path=None)
        key = ThreadKey(scope="owner", entity="mira", scene="dm", purpose="chat")
        first = store.attach(key, now=1000.0, window=60.0,
                             lane=SourceClass.INTERACTIVE).thread
        first.carry_summary = "前に TRPG の話をした"
        second = store.attach(key, now=5000.0, window=60.0,   # 過窗 ⇒ 新世代
                              lane=SourceClass.INTERACTIVE).thread
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.carry_summary, "前に TRPG の話をした",
                         "換世代要繼承承接摘要（淡化 session 的前提）")


class TestRollingWindow(unittest.TestCase):
    def test_big_budget_keeps_many_turns(self):
        messages = build_messages(request(rows(40)), max_chars=36000)
        history = [item for item in messages if item["role"] in {"user", "assistant"}][:-1]
        self.assertGreater(len(history), 20, "36K 預算不該只放 8 條")

    def test_small_budget_keeps_only_recent(self):
        messages = build_messages(request(rows(40, chars=300)), max_chars=2000)
        history = [str(item["content"]) for item in messages if "あ" in str(item["content"])]
        self.assertTrue(history, "至少要放進最近的")
        self.assertIn("039-", history[-1], "留下的必須是最新的")
        self.assertNotIn("000-", " ".join(history), "最舊的應該被擠掉")

    def test_oversized_message_is_clipped(self):
        huge = [{"role": "user", "text": "長" * 30000},
                {"role": "assistant", "text": "短"}]
        messages = build_messages(request(huge), max_chars=36000)
        clipped = [item for item in messages if "…（省略）" in str(item.get("content"))]
        self.assertTrue(clipped, "超長單條要標記截斷")

    def test_total_stays_within_budget(self):
        messages = build_messages(request(rows(40, chars=300)), max_chars=36000)
        total = sum(len(str(item.get("content") or "")) for item in messages)
        self.assertLessEqual(total, 36000, f"超預算：{total}")

    def test_no_orphan_assistant_at_the_cut(self):
        # 預算剛好只夠放「最新的一條 assistant」⇒ 它應該被丢掉，而不是變成無提問的回答
        history = [{"role": "user", "text": "長" * 4000},
                   {"role": "assistant", "text": "答" * 1500}]
        messages = build_messages(request(history, text="次は？"), max_chars=2600)
        # 去頭（所有 system 區塊：記憶／筆記／utterance 讀法…）去尾（本輪那句）
        talk = [item for item in messages if item.get("role") in {"user", "assistant"}]
        kept_history = talk[:-1]
        self.assertTrue(kept_history, f"應該還有東西：{messages}")
        self.assertEqual(kept_history[0]["role"], "user",
                         f"不該以孤立的 assistant 開頭：{[m['role'] for m in messages]}")

    def test_whole_transcript_is_offered_to_the_window(self):
        """PersonaStage 不再自己砍成 8 條（條數交給預算決定）。"""
        from agent.brain.compose.stages import PersonaStage
        import inspect
        source = inspect.getsource(PersonaStage.run)
        self.assertNotIn("transcript)[-8:]", source, "不要在 stage 層固定 8 條")


if __name__ == "__main__":
    unittest.main(verbosity=2)
