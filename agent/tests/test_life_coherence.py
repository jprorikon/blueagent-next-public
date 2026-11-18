#!/usr/bin/env python3
"""她自己做的事要「讀得懂」（owner 2026-10-27）：進度／連貫／不冒充／跨活動延續。

對應四條改動：
  ① 每一步吃得到前面幾步的結果（不然「把那一條重寫成一句」不知道是哪一條）；
  ② 日誌標出「第幾步／共幾步＋步驟名」，一件事在時間線上是一條線；
  ③ 沒做成照實寫「（這一步沒做成：原因）」，不拿步驟指示冒充結果；
  ④ 上一次「還開著」的那套，下一次優先接著做，並把上次最後一句帶進第一步。
"""

from __future__ import annotations

import pathlib
import tempfile
import time
import unittest

from agent.brain.domain.activity import Activity
from agent.brain.runtime.activity_log import ActivityLog
from agent.brain.runtime.life import (Preset, Rotation, build_activity, presets_for)
from agent.brain.runtime.life_step import _looks_open, make_step_fn


class FakeEntities:
    def get(self, entity):
        return type("E", (), {"name": "ミラ", "id": entity})()


def make_activity(steps=("第一步", "第二步", "第三步"), title="翻筆記"):
    return Activity.create("mira", title, list(steps), kind="study",
                           visibilities=["public"] * len(steps))


class TestOpenMarks(unittest.TestCase):
    def test_unfinished_sounding_close_is_open(self):
        self.assertTrue(_looks_open(["查了資料", "還是沒弄懂節奏那段"]))
        self.assertFalse(_looks_open(["查了資料", "抄下三條能用"]))
        self.assertFalse(_looks_open([]))


class TestRotationContinuity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "life-rotation.json"

    def test_close_roundtrip_and_expiry(self):
        rotation = Rotation(self.path)
        rotation.note_close("mira", "key-a", close="還沒弄懂", is_open=True, now=1000.0)
        row = rotation.last_close("mira", "key-a", now=1000.0)
        self.assertEqual((row["close"], row["open"]), ("還沒弄懂", True))
        self.assertIsNone(rotation.last_close("mira", "key-a", now=1000.0 + 25 * 3600))

    def test_pick_prefers_the_open_activity_and_carries_the_line(self):
        rotation = Rotation(self.path)
        presets = presets_for("mira")
        # 最後一套現在是「每日報」（once_per_day）／「技能整理」（every_days=7）——兩種都有自己的排程，
        # 不受延續規則支配（剛挑過就七天內不再排），這裡挑真正的一般活動
        target = [item for item in presets if not getattr(item, "once_per_day", False)
                  and not int(getattr(item, "every_days", 0) or 0)][-1]
        rotation.note_close("mira", target.key, close="還是沒弄懂敘事節奏那段",
                            is_open=True, now=time.time())
        # hour=10 ⇒ 不在每日報時段（21–24），這裡只驗「還開著的那套優先」
        chosen = rotation.pick("mira", presets, now=time.time(), hour=10)
        self.assertEqual(chosen.key, target.key, "還開著的那套要優先接著做")
        self.assertIn("沒弄懂", rotation.take_continuation())
        activity = build_activity("mira", rotation=rotation, hour=10)
        self.assertTrue(activity.meta.get("continuation"))

    def test_closed_activity_is_not_preferred(self):
        rotation = Rotation(self.path)
        presets = presets_for("mira")
        rotation.note_close("mira", presets[0].key, close="做完了", is_open=False,
                            now=time.time())
        # hour=10 ⇒ 不在每日報時段（21–24），這裡只驗「還開著的那套優先」
        chosen = rotation.pick("mira", presets, now=time.time(), hour=10)
        self.assertIn(chosen.key, {preset.key for preset in presets})
        self.assertEqual(rotation.take_continuation(), "",
                         "已經收尾的那套不該被當成延續")


class TestStepLogging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.log = ActivityLog(self.root / "logs")

    def _run(self, work, steps=("挑一條", "把它重寫成一句", "收尾")):
        activity = make_activity(steps)
        step_fn = make_step_fn(None, FakeEntities(), 0.0, log=self.log,
                               notes_root=self.root / "notes", work=work)
        out = []
        for step in activity.steps:
            activity.cursor = activity.steps.index(step)
            out.append(step_fn(activity, step))
        return activity, out

    def test_line_shows_progress_and_step_name(self):
        _, _out = self._run(lambda title, step, **kwargs: {"ok": True, "text": "結果一句",
                                                           "trace": []})
        rows = [row["text"] for row in self.log.entries("mira", limit=10)]
        self.assertIn("1/3 挑一條：結果一句", rows[0])
        self.assertIn("2/3 把它重寫成一句：結果一句", rows[1])
        self.assertIn("3/3 ", rows[2])
        self.assertIn("完了", rows[2], "最後一步才說完成")

    def test_failure_does_not_echo_the_instruction(self):
        _, _out = self._run(lambda title, step, **kwargs: {"ok": False,
                                                           "reason": "no_result"})
        rows = [row["text"] for row in self.log.entries("mira", limit=10)]
        self.assertTrue(all("この一歩はできなかった" in row for row in rows))
        self.assertFalse(any(row.endswith("挑一條") for row in rows),
                         "沒做成時不能讓步驟指示假裝是結果")

    def test_history_reaches_the_worker(self):
        seen: list[list[str]] = []

        def work(title, step, **kwargs):
            seen.append(list(kwargs.get("history") or ()))
            return {"ok": True, "text": f"{step}的結果", "trace": []}

        self._run(work)
        self.assertEqual(seen[0], [])
        self.assertTrue(any("挑一條" in item for item in seen[1]),
                        "第二步要看得到第一步做了什麼")


if __name__ == "__main__":
    unittest.main(verbosity=2)
