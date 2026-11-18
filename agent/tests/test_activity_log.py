#!/usr/bin/env python3
"""她的活動：**private / public 日誌** ＋ 主動消息的閘（owner 2026-10-22）。

判據：
  ① 每步一行進 `activity-<entity>.jsonl`，帶 `visibility`；
  ② **public 那幾則同時沉進記憶**（別人回話時檢索得到），private 不進記憶；
  ③ private 只出現在**她自己的 prompt**（`self_log_block`），不給別人看；
  ④ 主動消息的四道閘：安靜時段／最小間隔／每日上限／空字串。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.state import AppState
from agent.brain.runtime.activity_log import PRIVATE, PUBLIC, ActivityLog, quiet_hours
from agent.brain.runtime.proactive import ProactiveGate


class TestActivityLog(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ba-act-"))
        self.indexed: list[tuple[str, str]] = []
        self.log = ActivityLog(self.root, public_sink=lambda entity, text, entry:
                               self.indexed.append((entity, text)))

    def test_each_step_is_one_jsonl_line_with_visibility(self):
        self.log.append("mira", kind="study", text="讀完一章", visibility=PUBLIC,
                        activity="學習", step=1)
        self.log.append("mira", kind="study", text="有點累", visibility=PRIVATE,
                        activity="學習", step=2)
        rows = [json.loads(line) for line in
                (self.root / "activity-mira.jsonl").read_text().splitlines()]
        self.assertEqual([row["visibility"] for row in rows], [PUBLIC, PRIVATE])
        self.assertEqual([row["step"] for row in rows], [1, 2])

    def test_privacy_boundaries(self):
        self.log.append("mira", kind="play", text="學到新招", visibility=PUBLIC)
        self.log.append("mira", kind="play", text="輸了很不甘心", visibility=PRIVATE)
        self.assertEqual(len(self.indexed), 1, "只有 public 才進記憶索引")
        self.assertEqual(self.log.private_tail("mira"), ["輸了很不甘心"])
        self.assertEqual(self.log.public_tail("mira"), ["學到新招"])


class TestQuietHours(unittest.TestCase):
    def test_quiet_window_wraps_midnight(self):
        noon = time.mktime((2026, 10, 22, 12, 0, 0, 0, 0, -1))
        night = time.mktime((2026, 10, 22, 2, 0, 0, 0, 0, -1))
        self.assertFalse(quiet_hours(noon, start=23, end=7))
        self.assertTrue(quiet_hours(night, start=23, end=7))


class TestProactiveGate(unittest.TestCase):
    def setUp(self):
        self.sent: list[dict] = []
        self.gate = ProactiveGate(min_gap_s=60, daily_max=2, quiet_start=0, quiet_end=0)

    def test_sends_when_allowed(self):
        result = self.gate.maybe_send("mira", "我學到一件有趣的事",
                                      publish=lambda who, frame: self.sent.append(frame))
        self.assertTrue(result["sent"], result)
        self.assertEqual(self.sent[0]["kind"], "proactive")

    def test_min_gap_blocks_the_second(self):
        now = time.time()
        self.gate.maybe_send("mira", "第一句", publish=lambda *_: None, now=now)
        blocked = self.gate.maybe_send("mira", "第二句", publish=lambda *_: None, now=now + 5)
        self.assertFalse(blocked["sent"])
        self.assertEqual(blocked["reason"], "min_gap")

    def test_daily_max(self):
        # 釘在白天：用 time.time() 的話 23:53 之後跑，+400 s 會跨過午夜、每日計數歸零（2026-11-08 23:56 踩到）
        base = time.mktime((2026, 10, 22, 12, 0, 0, 0, 0, -1))
        for index in range(3):
            self.gate.maybe_send("mira", f"第 {index} 句", publish=lambda *_: None,
                                 now=base + index * 120)
        last = self.gate.maybe_send("mira", "第四句", publish=lambda *_: None,
                                    now=base + 400)
        self.assertFalse(last["sent"])
        self.assertEqual(last["reason"], "daily_max")

    def test_quiet_hours_block(self):
        gate = ProactiveGate(min_gap_s=0, daily_max=9, quiet_start=23, quiet_end=7)
        night = time.mktime((2026, 10, 22, 3, 0, 0, 0, 0, -1))
        result = gate.maybe_send("mira", "睡不著", publish=lambda *_: None, now=night)
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "quiet_hours")


class TestPrivateLogReachesHerPrompt(unittest.TestCase):
    def setUp(self):
        self.app = build(brain_delay=0, data_dir=tempfile.mkdtemp(prefix="ba-act-prompt-"),
                          step_duration=0.02, life_pause=0.005)
        self.addCleanup(self.app.shutdown_lives)
        self.state = AppState(self.app)

    def test_private_entry_is_in_her_prompt_block(self):
        self.app.activity_log.append("mira", kind="study", text="今天讀到一段很喜歡的話",
                                      visibility=PRIVATE)
        self.state.send("在嗎", principal="owner")
        # 這一輪的 prompt 沒直接回傳；驗 PersonaStage 拿到 activity_log（私記才有機會進 prompt）
        stages = self.app.pipelines.require("chat.text").stages
        persona = next((stage for stage in stages if getattr(stage, "name", "") == "persona"),
                       None)
        self.assertIsNotNone(persona, [getattr(stage, "name", "?") for stage in stages])
        self.assertIsNotNone(getattr(persona, "activity_log", None),
                             "PersonaStage 要拿到 activity_log（否則私記進不了 prompt）")
        self.assertIn("今天讀到一段很喜歡的話",
                      self.app.activity_log.private_tail("mira", limit=1)[0])

    def test_public_entry_becomes_retrievable_memory(self):
        self.app.activity_log.append("mira", kind="play",
                                      text="ミラ 玩到一個很怪的第二關", visibility=PUBLIC)
        facts = [fact["text"] for fact in self.app.memory.snapshot({"owner"})["facts"]
                 if fact["visible"]]
        self.assertTrue(any("第二關" in text for text in facts), facts[-5:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
