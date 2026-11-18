#!/usr/bin/env python3
"""各自的生物钟（owner 2026-11-08：「导入动态生物钟，让她们各自有各自的休息时间」）。

判据：
  ① 每个角色的作息窗**不一样**（不是全局 23–07 一刀切）；
  ② 同一天内窗口固定（反复调用不会一小时变一次）；
  ③ 状态四段：awake／winding（睡前 1 小时）／asleep／waking（醒后 30 分钟）；
  ④ 动态：熬过入睡点 1 小时 ⇒ 第二天整体后移（每次 +20 分、上限 +90 分）；
     按时睡就每天回 5 分；
  ⑤ 落盘：重开同一天读到同一个窗口。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent" / "tests"))

from agent.brain.runtime.inner.circadian import (LATE_SHIFT_STEP, MAX_SHIFT, CircadianClock, build_clocks)

NOON = (2026, 11, 8, 12, 0, 0)


def at(*parts, base=NOON):
    """以 2026-11-08 12:00 为基准偏移（分钟）。"""
    y, mo, d, h, mi, s = base
    return __import__("time").mktime((y, mo, d, h, mi, s, 0, 0, -1)) + parts[0] * 60.0


class CircadianCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-circadian-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def clock_for(self, entity: str) -> CircadianClock:
        return CircadianClock(entity, state_dir=self.root)

    def test_each_entity_has_its_own_window(self):
        windows = {who: self.clock_for(who).window(at(0)) for who in ("mira", "aster", "nori")}
        sleeps = {who: window["sleep_min"] for who, window in windows.items()}
        self.assertEqual(len(set(sleeps.values())), 3, f"三个人的入睡点必须不同：{sleeps}")
        # 用「距正午多少分钟」比较才跨得过午夜：ノリ 最早睡（23 点台），ミラ 最晚（深夜）
        def after_noon(minutes: int) -> int:
            return (int(minutes) - 12 * 60) % (24 * 60)

        order = {who: after_noon(minutes) for who, minutes in sleeps.items()}
        self.assertLess(order["nori"], order["aster"], order)
        self.assertLess(order["aster"], order["mira"], order)

    def test_window_is_pinned_within_the_same_day(self):
        clock = self.clock_for("mira")
        first = clock.window(at(0))
        second = clock.window(at(180))
        self.assertEqual(first["sleep_min"], second["sleep_min"])
        reopened = CircadianClock("mira", state_dir=self.root).window(at(240))
        self.assertEqual(first["sleep_min"], reopened["sleep_min"], "重启也要读到同一个窗口")

    def test_states_cover_asleep_winding_waking_awake(self):
        clock = self.clock_for("aster")
        window = clock.window(at(0))
        self.assertEqual(clock.state(window["sleep_at"] + 60), "asleep")
        # REVIEW 2026-11-08 A8：醒來**之後**的 30 分鐘才是 waking；醒前一刻仍是 asleep
        self.assertEqual(clock.state(window["wake_at"] - 10), "asleep")
        self.assertEqual(clock.state(window["wake_at"] + 60), "waking")
        self.assertEqual(clock.state(window["wake_at"] + 31 * 60), "awake")
        self.assertEqual(clock.state(window["sleep_at"] - 30), "winding")
        self.assertTrue(clock.settling_in(window["sleep_at"] - 30))
        self.assertFalse(clock.settling_in(window["wake_at"] + 31 * 60))
        self.assertTrue(clock.asleep(window["sleep_at"] + 600))
        self.assertFalse(clock.asleep(window["wake_at"] + 600))

    def test_late_activity_shifts_the_next_day(self):
        clock = self.clock_for("mira")
        window = clock.window(at(0))
        # 熬到入睡点之后 90 分钟才收工
        shift = clock.note_activity_end(window["sleep_at"] + 90 * 60)
        self.assertEqual(shift, LATE_SHIFT_STEP)
        tomorrow = clock.window(at(24 * 60))
        self.assertGreaterEqual(tomorrow["sleep_min"], window["sleep_min"], "第二天要更晚睡")

    def test_shift_is_clamped_and_settles_back(self):
        clock = self.clock_for("nori")
        window = clock.window(at(0))
        for _ in range(20):
            clock.note_activity_end(window["sleep_at"] + 120 * 60)
        self.assertLessEqual(clock.snapshot(at(0))["shift_min"], MAX_SHIFT)
        before = clock.snapshot(at(0))["shift_min"]
        clock.settle(at(0))
        self.assertEqual(clock.snapshot(at(0))["shift_min"], max(0, before - 5),
                         "按时睡一天要回 5 分")

    def test_settle_applies_once_per_day(self):
        """A6：LifeLoop 每一跳都會叫 settle，一天只能回 5 分（不然一個下午就歸零）。"""
        clock = self.clock_for("nori")
        window = clock.window(at(0))
        for _ in range(3):
            clock.note_activity_end(window["sleep_at"] + 120 * 60)
        before = clock.snapshot(at(0))["shift_min"]
        for _ in range(10):
            clock.settle(at(0))
        self.assertEqual(clock.snapshot(at(0))["shift_min"], before - 5)
        clock.settle(at(24 * 60))                     # 第二天再回 5
        self.assertEqual(CircadianClock("nori", state_dir=self.root).snapshot(at(24 * 60))["shift_min"],
                         before - 10, "跨重啟也記得今天回過了")

    def test_shared_clock_is_thread_safe(self):
        """LifeLoop／主動開口／adapter 共用同一個鐘：同時釘新一天的窗不能互相踩檔（REVIEW 2026-11-08）。"""
        import threading
        clock = self.clock_for("aster")
        errors = []

        def hammer(offset):
            try:
                for day in range(20):
                    clock.state(at(day * 24 * 60 + offset))
                    clock.settle(at(day * 24 * 60 + offset))
            except Exception as exc:                   # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])

    def test_early_finish_does_not_shift(self):
        clock = self.clock_for("aster")
        window = clock.window(at(0))
        self.assertEqual(clock.note_activity_end(window["sleep_at"] - 120 * 60), 0)

    def test_snapshot_is_human_readable(self):
        snap = self.clock_for("mira").snapshot(at(0))
        self.assertEqual(snap["entity"], "mira")
        self.assertIn(snap["state"], {"awake", "winding", "asleep", "waking"})
        self.assertRegex(snap["sleep"], r"^\d{2}:\d{2}$")
        self.assertRegex(snap["wake"], r"^\d{2}:\d{2}$")

    def test_build_clocks_makes_one_per_entity(self):
        class Item:
            def __init__(self, ident):
                self.id = ident
                self.name = ident

        class Entities:
            def all(self):
                return (Item("mira"), Item("aster"), Item("nori"))

        clocks = build_clocks(Entities(), self.root / "circadian")
        self.assertEqual(sorted(clocks), ["aster", "mira", "nori"])
        self.assertNotEqual(clocks["nori"].window(at(0))["sleep_min"],
                            clocks["mira"].window(at(0))["sleep_min"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
