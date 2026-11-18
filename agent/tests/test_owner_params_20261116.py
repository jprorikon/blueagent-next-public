#!/usr/bin/env python3
"""owner 2026-11-16 拍板的三個參數，預設值要照定的走（PENDING A.3／A.10）。

  · A.10 ① 主人對話裡生活流最多 4 格（`BLUEAGENT_RECALL_LIFE` 沒設時）；
  · A.10 ② 生活流單條（她的筆記 `self_note`）保留 14 天；
  · A.3   事件型事實預設 3 天（模型沒給 `valid_days` 時）。
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.stages import recall
from agent.brain.memory import facts
from agent.brain.runtime.memory import distill_support


class TestDefaults(unittest.TestCase):
    def test_values(self):
        self.assertEqual(recall.LIFE_SLOTS, 4)
        if "BLUEAGENT_LIFE_TTL_DAYS" not in os.environ:
            self.assertEqual(facts.LIFE_TTL_DAYS, 14.0)
        if "BLUEAGENT_EVENT_TTL_DAYS" not in os.environ:
            self.assertEqual(distill_support.EVENT_TTL_DAYS, 3.0)
            self.assertEqual(distill_support.event_valid_to("event", 0, 1000.0), 1000.0 + 3 * 86400)
        self.assertEqual(distill_support.event_valid_to("event", 5, 1000.0), 1000.0 + 5 * 86400,
                         "模型給了天數就照它")
        self.assertEqual(distill_support.event_valid_to("state", 0, 1000.0), 0.0)

    def test_owner_turn_asks_for_four_life_rows(self):
        """沒設環境變數時，生活流那一路請求 4 條；字面沒命中就讓給對話流。"""
        old = os.environ.pop("BLUEAGENT_RECALL_LIFE", None)
        self.addCleanup(lambda: old is None or os.environ.__setitem__("BLUEAGENT_RECALL_LIFE", old))
        calls: list[dict] = []

        class Ports:
            def recall_detail(self, scope, text, *, limit=4, participants=(), stream="", game=None):
                calls.append({"limit": limit, "stream": stream})
                return []

        env = SimpleNamespace(text="こんにちは", event=SimpleNamespace(source="phone:owner"))
        recall.RecallStage()._merged(env, SimpleNamespace(ports=Ports()), None, {"owner"}, 8)
        life = next(call for call in calls if call["stream"] == facts.STREAM_LIFE)
        dialogue = [call for call in calls if call["stream"] == facts.STREAM_DIALOGUE]
        self.assertEqual(life["limit"], 4)
        self.assertEqual(dialogue[0]["limit"], 8, "生活流一條都沒撈到 ⇒ 名額全給對話流")


if __name__ == "__main__":
    unittest.main(verbosity=2)
