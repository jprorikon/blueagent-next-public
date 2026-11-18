#!/usr/bin/env python3
"""上下文兩層（owner 2026-11-03）：

  ① **與當前對象的歷史**：群聊線裡別人的發言不得混進「我們兩個的歷史」（`_split_by_counterpart`）；
  ② **in-time 現況**：她此刻在做什麼（`PersonaStage._presence` 讀 `activity_store`），
     讓「今なにしてるの？」答得出正在做的事，而不是翻舊帳。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent" / "tests"))

from agent.brain.compose.stages.persona import PersonaStage, _split_by_counterpart  # noqa: E402
from agent.brain.messages import build_messages  # noqa: E402
from agent.brain.domain.activity import Activity  # noqa: E402
from agent.brain.runtime.activity_store import ActivityStore  # noqa: E402


def _row(role, speaker="", text="x", addressed=""):
    return {"role": role, "speaker": speaker, "text": text, "addressed_to": addressed,
            "at": time.time() - 60, "index": 1}


class TestSplitByCounterpart(unittest.TestCase):
    def test_single_person_thread_is_untouched(self):
        rows = [_row("user", "owner"), _row("assistant"), _row("user", "owner")]
        mine, ambient = _split_by_counterpart(rows, principal="owner")
        self.assertEqual(mine, rows)
        self.assertEqual(ambient, [])

    def test_other_peoples_turns_become_background(self):
        rows = [_row("user", "owner", "ただいま"),
                _row("assistant", addressed="owner", text="おかえり"),
                _row("user", "guest-x", "ノリ、今日の観測は？"),
                _row("assistant", addressed="guest-x", text="ねえ、今日って何か")]
        mine, ambient = _split_by_counterpart(rows, principal="owner")
        self.assertEqual([row["text"] for row in mine], ["ただいま", "おかえり"])
        self.assertEqual([row["text"] for row in ambient],
                         ["ノリ、今日の観測は？", "ねえ、今日って何か"])


class TestPresenceBlock(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-presence-"))
        self.store = ActivityStore(self.root / "activities")
        self.stage = PersonaStage.__new__(PersonaStage)      # 只測這一段，不建整條管線
        self.stage.activity_store = self.store

    def test_running_activity_shows_progress_and_next_step(self):
        activity = Activity.create("mira", "ルールブックを読む",
                                   ["目次を読む", "判定の章を読む", "まとめる"], kind="study")
        activity.start(now=time.time() - 300)
        activity.advance(now=time.time() - 240, note="done: 目次")
        self.store.save(activity)
        block = self.stage._presence("mira")
        self.assertIn("ルールブックを読む", block)
        self.assertIn("2/3", block)
        self.assertIn("判定の章を読む", block)          # いまの一手
        self.assertIn("さっき終えた", block)             # 直前の一歩
        self.assertIn("あなた自身の今の様子", block)

    def test_no_activity_says_so_instead_of_inventing(self):
        block = self.stage._presence("mira")
        self.assertIn("何も進めていない", block)

    def test_missing_store_means_no_block(self):
        self.stage.activity_store = None
        self.assertEqual(self.stage._presence("mira"), "")


class TestRendering(unittest.TestCase):
    def _request(self, **meta):
        from agent.brain.ports import BrainRequest
        return BrainRequest(system="SYS", text="今なにしてるの？", principal="owner",
                            entity="mira", thread_id="th", mode="chat", meta=meta)

    def test_presence_and_ambient_are_rendered(self):
        request = self._request(
            presence_block="### いま\n- 手を動かしている：ノートを読み返す（study・2/4）",
            ambient=[{"role": "user", "speaker": "guest-x", "text": "ノリ、元気？",
                      "at": time.time() - 120}],
            names={"guest-x": "ゲストX"}, now=time.time())
        blob = "\n".join(str(message.get("content") or "")
                         for message in build_messages(request))
        self.assertIn("手を動かしている：ノートを読み返す", blob)
        self.assertIn("[[ambient]]", blob)
        self.assertIn("ゲストX：ノリ、元気？", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
