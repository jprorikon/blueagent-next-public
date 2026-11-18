#!/usr/bin/env python3
"""分級輪轉：按天切 ＋ 保留期（owner 2026-10-22「轮转分级做掉」）。

為什麼不是只有大小輪轉：大小只知道「裝滿了」，不知道「多久以前」。
排查要看「昨天那段」、記憶要留「上個月那段」⇒ 必須按天切；保留期才擋得住長成幾十 GB。

分級（預設）：attention 14 天／activity 30／turns 30／episodes 90。
判據：
  ① 跨日才切（同一天內不動）；切出來的檔名帶日期，搬進 `daily/`；
  ② 活檔切完是 0 byte 的新檔（路徑不變，寫入端不用改）；
  ③ 超過保留期的日檔被刪；沒過期的不動；
  ④ 同一天重複切不會蓋掉既有日檔（併進去）；
  ⑤ 保留期設 0 ＝ 完全不切（可回退）。
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.ops.rotation import (DailyGuard, daily_keep_days, daily_summary,
                                            day_key, prune_daily, rotate_daily)


def write_dated(path: pathlib.Path, stamp: str, rows: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join('{"row":%d}\n' % index for index in range(rows)),
                    encoding="utf-8")
    when = time.mktime(time.strptime(stamp, "%Y%m%d")) + 3600
    os.utime(path, (when, when))


class TestDailyRotate(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.live = self.root / "attention.jsonl"

    def test_old_file_is_moved_into_daily_with_date(self):
        write_dated(self.live, "20261020")
        moved = rotate_daily(self.live, keep_days=14)
        self.assertEqual(len(moved), 1, moved)
        self.assertEqual(moved[0].name, "attention-20261020.jsonl")
        self.assertEqual(moved[0].parent.name, "daily")
        self.assertEqual(self.live.read_text(encoding="utf-8"), "", "活檔要變空的新檔")

    def test_todays_file_is_not_rotated(self):
        write_dated(self.live, day_key())
        self.assertEqual(rotate_daily(self.live, keep_days=14), [])
        self.assertTrue(self.live.read_text(encoding="utf-8"), "今天的資料留在活檔")

    def test_zero_keep_days_disables_it(self):
        write_dated(self.live, "20260101")
        self.assertEqual(rotate_daily(self.live, keep_days=0), [])
        self.assertTrue(self.live.stat().st_size)

    def test_same_day_does_not_overwrite_existing_archive(self):
        write_dated(self.live, "20261020", rows=2)
        rotate_daily(self.live, keep_days=14)
        write_dated(self.live, "20261020", rows=3)
        rotate_daily(self.live, keep_days=14)
        archive = self.root / "daily" / "attention-20261020.jsonl"
        self.assertEqual(len(archive.read_text(encoding="utf-8").splitlines()), 5,
                         "同一天兩次切檔要合併，不能蓋掉")

    def test_empty_live_file_is_left_alone(self):
        self.live.write_text("", encoding="utf-8")
        os.utime(self.live, (time.time() - 86400 * 5,) * 2)
        self.assertEqual(rotate_daily(self.live, keep_days=14), [])


class TestPrune(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.live = self.root / "attention.jsonl"
        self.live.write_text("", encoding="utf-8")
        self.daily = self.root / "daily"
        self.daily.mkdir()
        # 日期要**相對今天**算：寫死 2026-10-01／10-10 的話，跑到 10-25 之後
        # 「10-10」也過期了，這個測試就會莫名紅掉（2026-10-25 實測踩到）。
        self.old = day_key(time.time() - 30 * 86400)
        self.fresh = day_key(time.time() - 5 * 86400)
        for stamp in (self.old, self.fresh, day_key()):
            (self.daily / f"attention-{stamp}.jsonl").write_text('{"row":1}\n', encoding="utf-8")

    def test_expired_days_are_deleted(self):
        removed = prune_daily(self.live, keep_days=14)
        names = sorted(row.name for row in removed)
        self.assertEqual(names, [f"attention-{self.old}.jsonl"], removed)
        left = sorted(row.name for row in self.daily.iterdir())
        self.assertIn(f"attention-{self.fresh}.jsonl", left, "沒過期的不該删")
        self.assertIn(f"attention-{day_key()}.jsonl", left)

    def test_zero_keep_days_prunes_nothing(self):
        self.assertEqual(prune_daily(self.live, keep_days=0), [])

    def test_summary_reports_files_and_range(self):
        info = daily_summary(self.live)
        self.assertEqual(info["files"], 3)
        self.assertEqual(info["oldest"], self.old)
        self.assertEqual(info["newest"], day_key())
        self.assertGreater(info["bytes"], 0)


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.live = self.root / "episodes.jsonl"

    def test_guard_rotates_on_check_and_counts(self):
        write_dated(self.live, "20260901")
        guard = DailyGuard(self.live, kind="episodes", keep_days=90, every=1)
        guard.check()
        self.assertEqual(guard.rotations, 1)
        self.assertEqual(guard.summary()["files"], 1)
        self.assertEqual(guard.summary()["keep_days"], 90)

    def test_guard_is_cheap_until_every_n(self):
        write_dated(self.live, "20260101")
        guard = DailyGuard(self.live, kind="episodes", keep_days=90, every=64)
        for _ in range(10):
            self.assertFalse(guard.note_write(), "沒到 N 次不該去看日界")
        self.assertTrue(self.live.stat().st_size, "還沒切")

    def test_env_sets_keep_days_per_kind(self):
        os.environ["BLUEAGENT_ATTENTION_KEEP_DAYS"] = "7"
        self.addCleanup(os.environ.pop, "BLUEAGENT_ATTENTION_KEEP_DAYS", None)
        self.assertEqual(daily_keep_days("attention"), 7)
        self.assertEqual(daily_keep_days("episodes"), 90, "其他種類不受影響")


class TestWiringInRuntime(unittest.TestCase):
    def test_attention_and_episodes_have_daily_guards(self):
        state = make_state()
        self.addCleanup(state.shutdown)
        history = state.app.attention.history        # AttentionGate 底下那個紀錄器
        episodes = state.app.archive                 # EpisodeArchive
        self.assertIsNotNone(getattr(history, "_daily", None), "attention 要掛分級輪轉")
        self.assertIsNotNone(getattr(episodes, "_daily", None), "episodes 要掛分級輪轉")
        self.assertGreater(episodes._daily.keep_days, history._daily.keep_days,
                           "episodes 留得比 attention 久（90 vs 14）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
