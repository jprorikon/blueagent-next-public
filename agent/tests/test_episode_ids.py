#!/usr/bin/env python3
"""封片 id 的唯一性，與封片檔載入時的去重口徑（2026-10-27）。

背景：`new_episode_id` 原本是 `ep_<entity>-<線內序號>`，而序號是**每條線自己的**
（`thread.episode_index`）⇒ 每一條線的第 1 集都叫 `ep_mira-1`。
`data/episodes.jsonl` 實測 583 行只有 76 個唯一 id（`ep_mira-1` 出現 63 次），
載入時按 id 去重 ⇒ 重啟後記憶體裡的封片由 82 掉到 76，507 筆歷史看不到。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent.brain.domain.episode import CutReason, Episode, seal
from agent.brain.domain.event import SourceClass
from agent.brain.domain.ids import ThreadKey, new_episode_id, start_hash
from agent.brain.domain.thread import Thread, TurnRecord
from agent.brain.runtime.episodes import EpisodeArchive

KEY = ThreadKey(scope="owner", entity="mira", scene="together", purpose="chat")


def _episode(*, episode_id, thread, index=1, started=100.0, ended=200.0, summary=""):
    return Episode(id=episode_id, key=KEY, thread_id=thread, index=index,
                   reason=CutReason.IDLE, started_at=started, ended_at=ended,
                   turns=(), summary=summary)


def _archive(tmp, episodes, *, max_kept=512):
    path = Path(tmp) / "episodes.jsonl"
    path.write_text("\n".join(json.dumps(ep.to_dict(), ensure_ascii=False) for ep in episodes) + "\n",
                    encoding="utf-8")
    return EpisodeArchive(path=path, max_kept=max_kept)


def _thread(thread_id, *, index=0, at=100.0):
    return Thread(key=KEY, id=thread_id, lane=SourceClass.INTERACTIVE,
                  created_at=at, last_active_at=at,
                  episode_index=index,
                  segment=[TurnRecord(turn_id="t_x", role="user", text="哈囉",
                                      source="phone:owner", at=at)])


class TestEpisodeId(unittest.TestCase):
    def test_same_index_in_different_threads_gets_different_ids(self):
        """★ 每一條線的第 1 集以前都叫 `ep_mira-1`。"""
        first = new_episode_id("mira", 1, "th_aaa-1")
        second = new_episode_id("mira", 1, "th_bbb-1")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("ep_mira-1-"), first)   # 舊前綴照留（測試/前端靠它）

    def test_legacy_call_without_thread_keeps_old_shape(self):
        self.assertEqual(new_episode_id("mira", 3), "ep_mira-3")

    def test_thread_suffix_is_sanitised(self):
        self.assertEqual(new_episode_id("mira", 2, "th_a b/c"), "ep_mira-2-th_a_b_c")
        self.assertEqual(new_episode_id("mira", 2, "--"), "ep_mira-2")

    def test_seal_stamps_the_thread_into_the_id(self):
        """封片要真的把線帶進去（不是只有 id 函式會做）。"""
        left = seal(_thread("th_aaa-1"), 200.0, CutReason.IDLE)
        right = seal(_thread("th_bbb-1"), 200.0, CutReason.IDLE)
        self.assertIsNotNone(left)
        self.assertIsNotNone(right)
        self.assertEqual(left.id, f"ep_mira-1-th_aaa_1-{start_hash(100.0)}")
        self.assertNotEqual(left.id, right.id)

    def test_regressed_index_in_the_same_thread_still_gets_a_new_id(self):
        """★ 線內序號在封片時就寫進檔案，要等這一輪結束才存盤 ⇒ 中間崩潰，重啟後
        同一條線會再發同一個序號（真檔有 2 組）。帶開始時刻，id 就不靠序號存盤。"""
        before_crash = seal(_thread("th_aaa-1", index=14, at=100.0), 200.0, CutReason.IDLE)
        after_restart = seal(_thread("th_aaa-1", index=14, at=300.0), 400.0, CutReason.IDLE)
        self.assertEqual(before_crash.index, after_restart.index)
        self.assertNotEqual(before_crash.id, after_restart.id)

    def test_same_episode_gets_the_same_id(self):
        """同線、同序號、同開始時刻＝同一集：id 必須是確定的（改寫版才蓋得回去）。"""
        self.assertEqual(new_episode_id("mira", 3, "th_a-1", 123.456),
                         new_episode_id("mira", 3, "th_a-1", 123.456))

    def test_start_hash_needs_a_thread_suffix(self):
        self.assertEqual(new_episode_id("mira", 3, "", 123.0), "ep_mira-3")
        self.assertEqual(new_episode_id("mira", 3, "th_a-1"), "ep_mira-3-th_a_1")

    def test_backfill_shares_the_rule_and_treats_new_ids_as_done(self):
        """回填工具與封片用同一條規則；新封的 id 一律算「已經是新格式」（重跑不改它）。"""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent" / "tools"))
        import backfill_episode_ids as backfill
        self.assertIs(backfill.short_hash, start_hash)
        sealed = seal(_thread("th_aaa-1", index=4, at=55.5), 99.0, CutReason.IDLE)
        self.assertTrue(backfill.is_migrated(sealed.id, "th_aaa_1"), sealed.id)


class TestReconcileIndexes(unittest.TestCase):
    """崩潰後 `threads.json` 的序號會落後封片檔 ⇒ 啟動時對一次帳（`index` 不再重號）。"""

    def test_index_is_raised_to_the_archive_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive(tmp, [_episode(episode_id="ep_mira-15-th_x_1-ab12",
                                              thread="th_x-1", index=15)])
            thread = _thread("th_x-1", index=14)
            stats = archive.reconcile_indexes([thread])
            self.assertEqual(thread.episode_index, 15)
            self.assertEqual(stats["bumped"], 1)

    def test_ahead_or_unknown_threads_are_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive(tmp, [_episode(episode_id="ep_mira-15-th_x_1-ab12",
                                              thread="th_x-1", index=15)])
            ahead = _thread("th_x-1", index=20)          # 已經在前面：不動
            other = _thread("th_y-1", index=3)           # 檔案裡沒有這條線：不動
            stats = archive.reconcile_indexes([ahead, other])
            self.assertEqual((ahead.episode_index, other.episode_index), (20, 3))
            self.assertEqual(stats["bumped"], 0)


class TestDefaultCap(unittest.TestCase):
    """記憶體上限：舊值 512 會把歷史擠出去（2026-10-27 之後預設調高）。"""

    def test_default_cap_holds_more_than_a_thousand(self):
        from agent.brain.runtime.episodes import DEFAULT_MAX_KEPT
        self.assertGreaterEqual(DEFAULT_MAX_KEPT, 1024)

    def test_load_keeps_every_episode_under_the_default_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [_episode(episode_id=f"ep_mira-{index}-th_x_1", thread="th_x-1",
                             index=index, started=float(index), ended=float(index) + 0.5)
                    for index in range(1, 601)]
            path = Path(tmp) / "episodes.jsonl"
            path.write_text("\n".join(json.dumps(row.to_dict(), ensure_ascii=False) for row in rows) + "\n",
                            encoding="utf-8")
            archive = EpisodeArchive(path=path)      # 不給 max_kept ⇒ 走預設
            self.assertEqual(len(archive.all()), 600)


class TestArchiveLoadDedup(unittest.TestCase):
    def test_legacy_shared_id_episodes_all_load(self):
        """★ 舊資料：同一個 id 出現在不同線 ⇒ 不能只留一筆。"""
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive(tmp, [_episode(episode_id="ep_mira-1", thread="th_a-1"),
                                     _episode(episode_id="ep_mira-1", thread="th_b-1"),
                                     _episode(episode_id="ep_mira-1", thread="th_c-1")])
            self.assertEqual(len(archive.all()), 3)
            self.assertEqual([ep.thread_id for ep in archive.all()], ["th_a-1", "th_b-1", "th_c-1"])

    def test_rewritten_episode_keeps_the_latest_revision(self):
        """同一集被改寫（同 id／同線／同開始時刻，ended_at 變了）⇒ 留最後一版，不留第一版。"""
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive(tmp, [
                _episode(episode_id="ep_mira-1-th_a", thread="th_a-1",
                         started=100.0, ended=200.0, summary="舊摘要"),
                _episode(episode_id="ep_mira-1-th_a", thread="th_a-1",
                         started=100.0, ended=260.0, summary="新摘要"),
            ])
            rows = archive.all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].summary, "新摘要")
            self.assertEqual(rows[0].ended_at, 260.0)

    def test_identical_duplicate_line_loads_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            same = _episode(episode_id="ep_mira-1-th_a", thread="th_a-1")
            archive = _archive(tmp, [same, same])
            self.assertEqual(len(archive.all()), 1)

    def test_same_thread_different_start_both_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = _archive(tmp, [
                _episode(episode_id="ep_mira-1", thread="th_a-1", started=100.0),
                _episode(episode_id="ep_mira-1", thread="th_a-1", started=900.0),
            ])
            self.assertEqual([ep.started_at for ep in archive.all()], [100.0, 900.0])

    def test_new_ids_do_not_collide(self):
        """新格式在真實形狀下（同角色、多條線、各自從 1 開始）不會撞。"""
        ids = [new_episode_id("mira", index, f"th_{name}-1")
               for name in ("aaa", "bbb", "ccc") for index in (1, 2)]
        self.assertEqual(len(set(ids)), len(ids))


if __name__ == "__main__":
    unittest.main()
