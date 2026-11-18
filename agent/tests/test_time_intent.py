#!/usr/bin/env python3
"""`memory.search` 的時間意圖（PENDING B.Agent.4 第三步，2026-11-16）。

判據：
  ① 具體日期認得出來：年月日／月日／斜線月日／上個月幾號／這個月幾號，簡繁日都吃；
  ② 沒寫年份的日期落在未來 ⇒ 當成去年；寫錯的日期（2 月 30 日）不認；
  ③ 回的是那一天的當地中午，且不超過現在；
  ④ 相對說法長的先比：「一昨日」是兩天前，不是被裡面的「昨日」吃成一天前；
  ⑤ 簡體寫法也算（「上个月」「刚才」以前不在詞表裡）；
  ⑥ 沒有時間點的「以前」只回 wants_past；什麼都沒有回 (None, False)；
  ⑦ `MemorySearch._time_intent` 走同一支。
"""

from __future__ import annotations

import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.memory import memory_search
from agent.brain.runtime.memory.time_intent import concrete_date, time_intent

#: 2026-11-16 15:00（當地）
NOW = time.mktime((2026, 11, 16, 15, 0, 0, 0, 0, -1))
DAY = 86400.0


def ymd(stamp: float) -> tuple[int, int, int, int]:
    moment = time.localtime(stamp)
    return moment.tm_year, moment.tm_mon, moment.tm_mday, moment.tm_hour


class TestConcreteDates(unittest.TestCase):
    def test_full_dates(self):
        for text in ("2026-11-03 のこと覚えてる？", "2026/11/3 那天", "2026年11月3日に話したこと"):
            with self.subTest(text):
                self.assertEqual(ymd(concrete_date(text, NOW)), (2026, 11, 3, 12))

    def test_month_day_in_three_languages(self):
        for text in ("11月3日のこと", "11月3号说了什么", "11月3號說了什麼", "11/3 に何話した"):
            with self.subTest(text):
                self.assertEqual(ymd(concrete_date(text, NOW)), (2026, 11, 3, 12))

    def test_relative_month_with_day(self):
        cases = {"上个月15号": (2026, 10, 15), "上個月15號": (2026, 10, 15),
                 "先月15日": (2026, 10, 15), "这个月3号": (2026, 11, 3),
                 "這個月3號": (2026, 11, 3), "今月3日": (2026, 11, 3)}
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(ymd(concrete_date(text, NOW))[:3], expected)

    def test_last_month_in_january_is_last_december(self):
        january = time.mktime((2027, 1, 10, 15, 0, 0, 0, 0, -1))
        self.assertEqual(ymd(concrete_date("先月20日", january))[:3], (2026, 12, 20))

    def test_future_month_day_means_last_year(self):
        self.assertEqual(ymd(concrete_date("12月24日のこと", NOW))[:3], (2025, 12, 24))
        self.assertEqual(ymd(concrete_date("11月16日", NOW))[:3], (2026, 11, 16), "今天不算未來")

    def test_invalid_or_absent_dates(self):
        for text in ("2月30日", "13月1日", "11/45", "小説の話", "3/4 くらいの量", ""):
            with self.subTest(text):
                if text == "3/4 くらいの量":
                    # 斜線月日認得出來是刻意的取捨：口語裡的分數很少跟記憶查詢一起出現
                    self.assertIsNotNone(concrete_date(text, NOW))
                else:
                    self.assertIsNone(concrete_date(text, NOW))

    def test_full_date_is_not_read_as_month_day(self):
        self.assertEqual(ymd(concrete_date("2025/12/24", NOW))[:3], (2025, 12, 24))


class TestTimeIntent(unittest.TestCase):
    def test_concrete_date_wins_and_never_exceeds_now(self):
        as_of, past = time_intent("11月3日に何を話した？", NOW)
        self.assertTrue(past)
        self.assertEqual(ymd(as_of)[:3], (2026, 11, 3))
        early = time.mktime((2026, 11, 16, 9, 0, 0, 0, 0, -1))
        self.assertEqual(time_intent("11月16日のこと", early)[0], early, "今天中午還沒到 ⇒ 取現在")

    def test_longest_hint_first(self):
        self.assertAlmostEqual(time_intent("一昨日の話", NOW)[0], NOW - 2 * DAY)
        self.assertAlmostEqual(time_intent("昨日の話", NOW)[0], NOW - 1 * DAY)

    def test_simplified_hints(self):
        self.assertAlmostEqual(time_intent("上个月说的", NOW)[0], NOW - 30 * DAY)
        self.assertAlmostEqual(time_intent("刚才那句", NOW)[0], NOW - 0.02 * DAY)

    def test_n_days_ago(self):
        self.assertAlmostEqual(time_intent("5日前に言った", NOW)[0], NOW - 5 * DAY)
        self.assertAlmostEqual(time_intent("2 days ago", NOW)[0], NOW - 2 * DAY)

    def test_past_without_point_and_nothing(self):
        self.assertEqual(time_intent("以前好きだったもの", NOW), (None, True))
        self.assertEqual(time_intent("小説の話", NOW), (None, False))

    def test_memory_search_uses_the_same_parser(self):
        search = memory_search.MemorySearch.__new__(memory_search.MemorySearch)
        as_of, past = search._time_intent("2026年11月3日")
        self.assertTrue(past)
        self.assertEqual(ymd(as_of)[:3], (2026, 11, 3))
        self.assertIn("昨天", memory_search.AS_OF_HINTS, "舊名字仍可從 memory_search 取到")


if __name__ == "__main__":
    unittest.main(verbosity=2)
