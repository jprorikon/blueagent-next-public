#!/usr/bin/env python3
"""生物钟接进三条线（owner 2026-11-08）：自主动作／主动开口／群聊「拿起手机」。

聊天不在这条约束里——主人说话她照样答；这套只管**她自己**的节奏。
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

from agent.adapter.app_server import GroupAttention  # noqa: E402
from agent.brain.domain.activity import Activity  # noqa: E402
from agent.brain.runtime.activity_store import ActivityStore  # noqa: E402
from agent.brain.runtime.inner.circadian import CircadianClock  # noqa: E402
from agent.brain.runtime.life_loop import LifeLoop  # noqa: E402
from agent.brain.runtime.proactive import ProactiveGate  # noqa: E402

NOON = (2026, 11, 8, 12, 0, 0)


def ts(minutes: float = 0.0) -> float:
    y, mo, d, h, mi, s = NOON
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1)) + minutes * 60.0


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-circadian-int-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.clock = CircadianClock("aster", state_dir=self.root)
        self.window = self.clock.night_window(ts(0))

    def test_proactive_gate_stays_quiet_while_asleep(self):
        gate = ProactiveGate(min_gap_s=0, daily_max=9, quiet_start=0, quiet_end=0,
                             circadian={"aster": self.clock}, clock=lambda: ts(0))
        ok, reason = gate.allows("aster", now=self.window["sleep_at"] + 1800)
        self.assertFalse(ok)
        self.assertEqual(reason, "circadian_sleep")
        awake = self.window["wake_at"] + 3 * 3600
        ok, _reason = gate.allows("aster", now=awake)
        self.assertTrue(ok, "醒了就可以主动开口")

    def test_group_attention_does_not_pick_up_the_phone_while_asleep(self):
        attention = GroupAttention(circadian={"aster": self.clock})
        night = self.window["sleep_at"] + 600
        asleep = attention.decision("aster", {"id": "m1", "created_at": night}, owner_recent=True)
        self.assertFalse(asleep.seen, "睡着时 owner 的地板也叫不醒")
        self.assertEqual(asleep.delay, 0.0)
        self.assertEqual(attention.probability("aster", night, call="at")[0], 0.3,
                         "@ 能叫醒，但按生物钟打折（GROUP-SELF-CHAT §12-3）")
        awake = self.window["wake_at"] + 3 * 3600
        self.assertEqual(attention.probability("aster", awake, call="at")[0], 0.95)


class LifeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-circadian-life-")
        self.addCleanup(self.tmp.cleanup)
        self.now = [ts(0)]
        self.clock = CircadianClock("mira", state_dir=pathlib.Path(self.tmp.name),
                                    clock=lambda: self.now[0])

    def test_life_loop_does_not_work_while_asleep_and_resumes_after_waking(self):
        store = ActivityStore(pathlib.Path(self.tmp.name) / "activities")
        steps: list[str] = []

        def step_fn(activity, step):
            steps.append(str(step))
            return "ok"

        window = self.clock.night_window(ts(0))
        self.now[0] = window["sleep_at"] + 1800          # 正睡着
        loop = LifeLoop("mira", store, step_fn, step_pause=0.01,
                        clock=lambda: self.now[0], circadian=self.clock)
        try:
            loop.start(Activity.create("mira", "夜里的活", ["一", "二"]))
            time.sleep(0.2)
            self.assertEqual(steps, [], "睡着的时候不该推进任何一步")
            self.assertFalse(loop._runnable())
            self.now[0] = window["wake_at"] + 600        # 醒了
            self.assertTrue(loop._runnable())
            deadline = time.time() + 3.0
            while time.time() < deadline and len(steps) < 2:
                time.sleep(0.02)
            self.assertEqual(len(steps), 2, "醒来要接着做完")
        finally:
            loop.shutdown()

    def test_no_new_activity_while_winding_down(self):
        """REVIEW 2026-11-08 A8：睡前一小時只收尾，做完手上這件不接下一件；醒透了才接。"""
        from agent.brain.domain.activity import ActivityStatus
        store = ActivityStore(pathlib.Path(self.tmp.name) / "activities-wind")
        window = self.clock.night_window(ts(0))
        loop = LifeLoop("mira", store, lambda activity, step: "ok", step_pause=0.01,
                        clock=lambda: self.now[0], circadian=self.clock,
                        next_activity=lambda entity, *_: Activity.create(entity, "次の活", ["一"]))
        try:
            finished = Activity.create("mira", "手上這件", ["一"])
            finished.status = ActivityStatus.DONE
            loop._activity = finished
            self.now[0] = window["sleep_at"] - 30 * 60        # winding
            self.assertFalse(loop._runnable(), "睡前不開新的一件")
            self.now[0] = window["wake_at"] + 10 * 60         # waking
            self.assertFalse(loop._runnable(), "剛醒也先不開新的")
            self.now[0] = window["wake_at"] + 3 * 3600        # 醒透了
            self.assertTrue(loop._runnable())
        finally:
            loop.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
