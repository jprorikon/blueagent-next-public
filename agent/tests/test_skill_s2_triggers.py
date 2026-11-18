#!/usr/bin/env python3
"""S2：技能的兩個觸發點（owner 2026-11-03）。

  ① **同一個任務失敗 ≥2 次** ⇒ 那一步的 prompt 會要求她先寫技能（「為什麼失敗／下次怎麼做」）；
  ② **每週整理活動**（`skills-tidy`）⇒ 一週一次，不會每天翻。

判據：
  · `on_failed` 把 `failures` 累加（不是只改狀態）；
  · `_failure_hint` 只在 ≥2 次時出字，且字裡有工具名與兩個必填段；
  · `Preset.every_days=7` ⇒ 7 天內 `pick()` 不會再挑到它，期滿才回來。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.domain.activity import StepStatus  # noqa: E402
from agent.brain.runtime.inner.task_ledger import TaskLedger  # noqa: E402
from agent.brain.runtime.life import PRESETS, Preset, Rotation  # noqa: E402
from agent.brain.runtime.life_step import _failure_hint  # noqa: E402


class _Activity:
    def __init__(self, meta):
        self.id = "act_1"
        self.meta = meta
        self.steps = []


class TestFailureTrigger(unittest.TestCase):
    def test_failures_accumulate(self):
        ledger = TaskLedger(tempfile.mkdtemp(), "nori")
        task = ledger.add("表をつくる")
        activity = _Activity({"task_id": task["id"]})
        ledger.on_failed(activity)
        ledger.on_failed(activity)
        self.assertEqual(ledger.failures_of(task["id"]), 2)
        self.assertEqual(ledger.get(task["id"])["state"], "hanging")

    def test_hint_only_after_two_failures(self):
        self.assertEqual(_failure_hint(_Activity({"failures": 1})), "")
        hint = _failure_hint(_Activity({"failures": 2}))
        self.assertIn("mcp_skills_write", hint)
        self.assertIn("完了条件", hint)
        self.assertIn("失敗時", hint)

    def test_hint_survives_missing_meta(self):
        self.assertEqual(_failure_hint(_Activity({})), "")


class TestWeeklyTidy(unittest.TestCase):
    def test_tidy_preset_is_weekly(self):
        for who, items in PRESETS.items():
            tidy = [p for p in items if p.key == "skills-tidy"]
            self.assertEqual(len(tidy), 1, f"{who} 少了 skills-tidy")
            self.assertEqual(tidy[0].every_days, 7, f"{who} 的整理活動不是每週")

    def test_pick_skips_recently_done_weekly_preset(self):
        presets = (Preset("daily-a", "毎日の作業", "study", (("a", "public"),)),
                   Preset("skills-tidy", "技能の整理", "study", (("b", "public"),),
                          every_days=7))
        rotation = Rotation(None, daily_cap=99, min_repeat_h=0)
        now = 1_700_000_000.0
        rotation._remember("nori", "skills-tidy", now - 86400)      # 昨天剛做過
        for _ in range(6):
            picked = rotation.pick("nori", presets, hour=12, now=now)
            self.assertEqual(picked.key, "daily-a")
        # 期滿（7 天後）就該輪得到它
        later = now + 7 * 86400
        keys = {rotation.pick("nori", presets, hour=12, now=later).key for _ in range(4)}
        self.assertIn("skills-tidy", keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
