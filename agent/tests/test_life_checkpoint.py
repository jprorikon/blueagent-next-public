#!/usr/bin/env python3
"""斷點續做（owner 2026-11-03）：

  ① 主人一開口 ⇒ 她在**安全邊界**停下，並把「第幾步／下一步／上一句結果」寫成 `meta["resume"]`；
  ② 主人走了 ⇒ **不立刻續**，先沉 `resume_beats` 拍（預設 3），到時才回到斷點；
  ③ 續做時 prompt 會帶「前回は話しかけられて中断した所から続き（前回ここまで：…）」。

這裡用「不啟 worker 執行緒」的方式建物件，只測狀態機本身（時鐘可注入）。
"""

from __future__ import annotations

import pathlib
import sys
import threading
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.domain.activity import Activity, ActivityStatus, StepStatus  # noqa: E402
from agent.brain.runtime.life_loop import LifeLoop  # noqa: E402


class _Store:
    def __init__(self) -> None:
        self.saved = 0

    def save(self, activity) -> None:
        self.saved += 1

    def load(self, entity):
        return None

    def clear(self, entity):
        return None


def _loop(activity, *, now: list[float], beats: int = 3, beat_s: float = 30.0) -> LifeLoop:
    loop = LifeLoop.__new__(LifeLoop)          # 不跑 __init__（那會起背景執行緒）
    loop.entity = "mira"
    loop.store = _Store()
    loop._activity = activity
    loop._paused = False
    loop._paused_by_human = False
    loop._human = 0
    loop._resume_beats = beats
    loop._beat_seconds = beat_s
    loop._resume_at = 0.0
    loop._cond = threading.Condition()
    loop._recovered = False
    loop._ledger = None
    loop._budget = None
    loop._next_activity = None
    loop._step_cost = 1.0
    loop._clock = lambda: now[0]
    loop.logs = []
    loop._log = loop.logs.append
    return loop


def _activity() -> Activity:
    activity = Activity.create("mira", "ノートを読み返す", ["読む", "写す", "まとめる"])
    activity.start(now=1000.0)
    activity.steps[0].note = "完了：二段落だけ読めた"
    activity.steps[0].status = StepStatus.DONE
    activity.cursor = 1
    return activity


class TestCheckpointResume(unittest.TestCase):
    def test_interrupt_writes_a_checkpoint(self):
        now = [1000.0]
        activity = _activity()
        loop = _loop(activity, now=now)
        self.assertTrue(loop._runnable())
        loop.human_enter()
        self.assertFalse(loop._runnable())              # 人在說話 ⇒ 不做
        resume = activity.meta["resume"]
        self.assertEqual(resume["cursor"], 1)
        self.assertEqual(resume["next_step"], "写す")
        self.assertIn("二段落だけ読めた", resume["last"])
        self.assertEqual(activity.status, ActivityStatus.PAUSED)

    def test_does_not_resume_immediately_after_human_leaves(self):
        now = [1000.0]
        activity = _activity()
        loop = _loop(activity, now=now, beats=3, beat_s=30.0)
        loop.human_enter()
        loop.human_exit()
        self.assertFalse(loop._runnable())              # 剛走：還在沉
        now[0] += 60.0                                  # 兩拍之後
        self.assertFalse(loop._runnable())
        now[0] += 40.0                                  # 三拍之後
        self.assertTrue(loop._runnable())
        self.assertEqual(activity.status, ActivityStatus.RUNNING)
        self.assertTrue(any("回到斷點" in line for line in loop.logs))

    def test_resume_beats_zero_means_back_immediately(self):
        now = [1000.0]
        activity = _activity()
        loop = _loop(activity, now=now, beats=0)
        loop.human_enter()
        loop.human_exit()
        self.assertTrue(loop._runnable())


class TestResumeInPrompt(unittest.TestCase):
    def test_progress_line_mentions_the_interruption(self):
        from agent.brain.runtime.life_step import _content_of
        activity = _activity()
        activity.meta["resume"] = {"cursor": 1, "next_step": "写す",
                                   "last": "二段落だけ読めた", "reason": "human"}
        seen: dict = {}

        def work(title, step, *, entity="", history=(), progress=""):
            seen["progress"] = progress
            return {"ok": True, "text": "続きを書いた", "trace": []}

        _content_of(activity, "ミラ", "写す", work=work, history=[], progress="2/3ステップ目")
        self.assertIn("中断した所から続き", seen["progress"])
        self.assertIn("二段落だけ読めた", seen["progress"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
