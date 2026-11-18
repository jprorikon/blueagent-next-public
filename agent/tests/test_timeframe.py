#!/usr/bin/env python3
"""時間感（`runtime/inner/timeframe.py`）：她分得出「さっき／3 分前／昨日」，也看得到輪次與跳數。"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from agent.brain.runtime.inner.timeframe import Timeline, human_age, stamp

# 鐘一律釘死：以前拿 `time.time()` 當「現在」，凌晨 00:00～00:01 跑就會掛
# （24h+60s 前落到前天、60 秒前那筆落在昨天），結果跟著牆上時間走（2026-10-29 00:00 JST 撞到）。
AFTERNOON = time.mktime((2026, 10, 29, 15, 0, 0, 0, 0, -1))
JUST_AFTER_MIDNIGHT = time.mktime((2026, 10, 29, 0, 0, 30, 0, 0, -1))


class TestHumanAge(unittest.TestCase):
    def test_short_is_just_now(self):
        self.assertEqual(human_age(5), "さっき")
        self.assertEqual(human_age(89), "さっき")

    def test_minutes_and_hours(self):
        self.assertEqual(human_age(3 * 60), "3 分前")
        self.assertEqual(human_age(2 * 3600 + 30), "2 時間前")

    def test_long_ago_uses_clock(self):
        now = AFTERNOON
        self.assertIn("昨日", human_age(24 * 3600 + 60, now=now))
        self.assertIn("月", human_age(3 * 24 * 3600, now=now))

    def test_stamp_wraps_age(self):
        now = 1_000_000.0
        self.assertEqual(stamp(now - 120, now=now), "（2 分前）")
        self.assertEqual(stamp(0, now=now), "")


class FakeThread:
    turns = 41
    last_active_at = 0.0


class FakeArchive:
    class _Episode:
        ended_at = 0.0
        entity = "mira"

    def __init__(self, now=AFTERNOON):
        self.now = now

    def latest(self, count=1):
        episode = self._Episode()
        episode.ended_at = self.now - 600
        return (episode,)


class FakeBeat:
    def snapshot(self):
        return {"ticks": 40, "interval_s": 45.0, "spoken": 4, "last_spoken_tick": 36}


class FakeBeats:
    def get(self, entity):
        return FakeBeat()


class FakeActivity:
    def __init__(self, now=AFTERNOON):
        self.now = now

    def entries(self, entity, *, limit=200, visibility=None):
        return [{"at": self.now - 60, "text": "a"}, {"at": self.now - 3 * 86400, "text": "b"}]


class TestTimelineBlock(unittest.TestCase):
    def test_block_lists_clock_line_turns_and_beats(self):
        thread = FakeThread()
        thread.last_active_at = AFTERNOON - 120
        block = Timeline(archive=FakeArchive(), heartbeats=FakeBeats(),
                         activity_log=FakeActivity(), clock=lambda: AFTERNOON).block("mira", thread)
        self.assertIn("### いま（時間感）", block)
        self.assertIn("現在：", block)
        self.assertIn("42 往復目", block)          # turns=41 ⇒ 這一輪是第 42
        self.assertIn("前の発言は2 分前", block)
        self.assertIn("エピソード", block)
        self.assertIn("今日（00:00 から）自分で 1 件やった", block)   # 3 天前那筆不算
        self.assertIn("40 拍目", block)
        # ★「距上次開口」＝40 − 36 ＝ 4 拍（以前寫成「總跳數 − 開口次數」＝36）
        self.assertIn("最後に話しかけてから 4 拍", block)

    def test_new_year_day_still_says_yesterday(self):
        now = time.mktime((2027, 1, 1, 10, 0, 0, 0, 0, -1))
        self.assertTrue(human_age(12 * 3600, now=now).startswith("昨日"))

    def test_unknown_last_spoken_is_left_out(self):
        class NeverSpoke(FakeBeat):
            def snapshot(self):
                return {"ticks": 40, "interval_s": 45.0, "spoken": 0, "last_spoken_tick": 0}

        class Beats:
            def get(self, entity):
                return NeverSpoke()

        block = Timeline(heartbeats=Beats()).block("mira", None)
        self.assertIn("40 拍目", block)
        self.assertNotIn("話しかけてから", block)

    def test_other_characters_episode_is_not_hers(self):
        class OnlyKei(FakeArchive):
            def latest(self, count=1):
                episode = self._Episode()
                episode.entity = "nori"
                episode.ended_at = self.now - 600
                return (episode,)

        block = Timeline(archive=OnlyKei()).block("mira", None, now=AFTERNOON)
        # 只剩標題＋「現在」兩行：ノリ剛封片不能寫成她的（以前取全域最後一集）
        self.assertEqual(len(block.splitlines()), 2, block)

    def test_bare_timeline_still_gives_the_clock(self):
        """只有「現在」也要給：她至少知道今天幾號、幾點（owner 2026-10-27 第 1 條）。"""
        block = Timeline().block("mira", None)
        self.assertIn("### いま（時間感）", block)
        self.assertIn("現在：", block)

    def test_missing_pieces_do_not_raise(self):
        class Broken:
            def latest(self, count=1):
                raise RuntimeError("boom")

        block = Timeline(archive=Broken()).block("mira", None)
        self.assertIn("現在：", block)


class TestJustAfterMidnight(unittest.TestCase):
    """負對照：同一組輸入搬到 00:00:30，日曆上本來就該換答案；釘了鐘的測試則不受牆上時間影響。"""

    def test_same_inputs_at_midnight_change_by_calendar(self):
        # 24h+60s 前＝10/27 23:59，是前天不是昨天——舊測試在凌晨掛的正是這個
        self.assertEqual(human_age(24 * 3600 + 60, now=JUST_AFTER_MIDNIGHT), "10月27日 23:59")
        # 60 秒前那筆在 10/28 23:59，不算「今日」
        block = Timeline(activity_log=FakeActivity(JUST_AFTER_MIDNIGHT)).block(
            "mira", None, now=JUST_AFTER_MIDNIGHT)
        self.assertIn("今日（00:00 から）自分で 0 件やった", block)

    def test_pinned_cases_ignore_the_wall_clock(self):
        # 牆上時間假裝是凌晨：釘了鐘的測試照樣要過（任何一處偷看 `time.time()` 都會在這裡露餡）
        with mock.patch("time.time", return_value=JUST_AFTER_MIDNIGHT):
            TestHumanAge("test_long_ago_uses_clock").test_long_ago_uses_clock()
            TestTimelineBlock("test_block_lists_clock_line_turns_and_beats") \
                .test_block_lists_clock_line_turns_and_beats()
            TestTimelineBlock("test_other_characters_episode_is_not_hers") \
                .test_other_characters_episode_is_not_hers()


if __name__ == "__main__":
    unittest.main(verbosity=2)
