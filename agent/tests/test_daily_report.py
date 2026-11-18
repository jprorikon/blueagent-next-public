#!/usr/bin/env python3
"""每日報（引擎內）＋ rotation 的「一天一次」規則。"""

from __future__ import annotations

import os
import time
import unittest

from agent.brain.runtime import daily_report
from agent.brain.runtime.life import Preset, Rotation, build_activity, presets_for


class TestDailyReportPreset(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("BLUEAGENT_REPORT_HOUR")
        os.environ["BLUEAGENT_REPORT_HOUR"] = "21"      # demo_common 預設把它關掉

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("BLUEAGENT_REPORT_HOUR", None)
        else:
            os.environ["BLUEAGENT_REPORT_HOUR"] = self._saved

    def test_every_entity_has_one_report_preset(self):
        for entity in ("mira", "aster", "nori"):
            keys = [item.key for item in presets_for(entity)]
            self.assertIn(daily_report.REPORT_KEY, keys)
            self.assertEqual(keys.count(daily_report.REPORT_KEY), 1)

    def test_preset_is_once_per_day_and_in_window(self):
        preset = [p for p in presets_for("nori") if p.key == daily_report.REPORT_KEY][0]
        self.assertTrue(preset.once_per_day)
        self.assertEqual(preset.hours, (21, 0))   # 21:00–24:00（跨午夜寫法）
        # 三個步驟：兩個公開、一個私密（最後一步只留給她自己）
        self.assertEqual(len(preset.steps), 3)
        self.assertEqual([vis for _, vis in preset.steps], ["public", "public", "private"])

    def test_can_be_disabled_or_moved_by_env(self):
        self.assertIsNone(daily_report.report_hour({"BLUEAGENT_REPORT_HOUR": "off"}))
        self.assertEqual(daily_report.report_hour({"BLUEAGENT_REPORT_HOUR": "7"}), 7)
        self.assertEqual(daily_report.report_hour({}), 21)
        # ★ 0 是午夜，不是關掉（以前 "0" 跟 off 同一組）
        self.assertEqual(daily_report.report_hour({"BLUEAGENT_REPORT_HOUR": "0"}), 0)
        self.assertIsNone(daily_report.report_hour({"BLUEAGENT_REPORT_HOUR": "-1"}))

    def test_bad_window_falls_back_instead_of_raising(self):
        self.assertEqual(daily_report._window_hours({"BLUEAGENT_REPORT_WINDOW_H": "abc"}), 3)
        self.assertEqual(daily_report._window_hours({"BLUEAGENT_REPORT_WINDOW_H": "0"}), 1)
        self.assertEqual(daily_report._window_hours({}), 3)


class TestRotationPicksReportOncePerDay(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("BLUEAGENT_REPORT_HOUR")
        os.environ["BLUEAGENT_REPORT_HOUR"] = "21"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("BLUEAGENT_REPORT_HOUR", None)
        else:
            os.environ["BLUEAGENT_REPORT_HOUR"] = self._saved

    def _library(self):
        return tuple(item for item in presets_for("aster"))

    @staticmethod
    def _noon(hour: int) -> float:
        """今天（本地時區）的某個整點。"""
        now = time.localtime()
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, 30, 0, 0, 0, -1))

    def test_prefers_report_inside_window_then_never_again_today(self):
        rotation = Rotation(path=None)          # 不落盤
        library = self._library()
        at = self._noon(21)
        first = rotation.pick("aster", library, now=at)
        self.assertEqual(first.key, daily_report.REPORT_KEY)
        # 同一天再挑 ⇒ 不會再是報告
        second = rotation.pick("aster", library, now=at + 60)
        self.assertNotEqual(second.key, daily_report.REPORT_KEY)
        # 隔天同一個時段 ⇒ 又可以報一次
        third = rotation.pick("aster", library, now=at + 86400)
        self.assertEqual(third.key, daily_report.REPORT_KEY)

    def test_outside_window_report_is_not_forced(self):
        rotation = Rotation(path=None)
        picked = rotation.pick("aster", self._library(), now=self._noon(10))
        self.assertNotEqual(picked.key, daily_report.REPORT_KEY)

    def test_build_activity_carries_report_meta_and_windows(self):
        rotation = Rotation(path=None)
        activity = build_activity("mira", rotation=rotation, hour=21)
        self.assertEqual(activity.meta.get("preset"), daily_report.REPORT_KEY)
        self.assertEqual(activity.meta.get("form"), "note")
        self.assertEqual(len(activity.steps), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
