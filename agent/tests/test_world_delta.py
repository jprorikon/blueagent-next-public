#!/usr/bin/env python3
"""世界變化（owner 2026-11-03 拍板 ②：來自外部提案的 State Diff）。

判據：
  ① 第一步之後再回來，一定先看到「前の一歩から ◯時間◯分」——時間感不是靠模型猜；
  ② 台帳報的是**差量**（跟上一拍比），不是每次都重念一遍總數；
  ③ 「這段期間有沒有人找她」「自己留了什麼」**只報事實**：沒接上就整行不寫；
  ④ 上一拍的時刻落盤 ⇒ 重啟後差距照樣算得對（不靠記憶體）。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))   # 倉庫根

from agent.brain.runtime.inner.world_delta import (  # noqa: E402
    SHARED_KEYS, SharedWorld, WorldDelta, _gap_text)
from prompt_lang import chinese_hits  # noqa: E402

#: 2026-11-03 05:20 UTC ＝ 14:20 JST（她的時區）
T0 = 1793683200.0
HOUR = 3600.0


class _Ledger:
    def __init__(self, rows) -> None:
        self.rows = rows

    def items(self):
        return list(self.rows)


def _delta(root, **kwargs) -> WorldDelta:
    kwargs.setdefault("notes_root", root / "notes")
    return WorldDelta(root / "world", "mira", tz="Asia/Tokyo", **kwargs)


def _shared(root, **kwargs) -> SharedWorld:
    return SharedWorld(root / "world", tz="Asia/Tokyo", **kwargs)


class _Thread:
    """假的線（只放共享層會讀的四個欄位）。"""

    def __init__(self, *, at: float, sources=(), participants=("owner",)) -> None:
        self.last_active_at = at
        self.sources = set(sources)
        self.participants = set(participants)


class _Threads:
    def __init__(self, rows) -> None:
        self.rows = list(rows)

    def threads(self):
        return tuple(self.rows)

    def recent_dialogue(self, entity, limit=40):
        return []


class TestSharedLayer(unittest.TestCase):
    def test_only_whitelisted_keys_are_stored(self):
        """共享層是白名單：私事一律不進來（塞得進去也留不住）。"""
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-shared-"))
        shared = _shared(root)
        facts = shared.sample(now=T0)
        self.assertEqual(set(facts) - set(SHARED_KEYS), set())
        self.assertEqual(facts["clock"], "14:20")
        self.assertIn("11月3日", facts["date"])
        # 手動塞一個不在白名單的鍵 ⇒ 讀回來會被剔掉
        shared.path().write_text(
            '{"at": %f, "clock": "14:20", "secret": "主人の秘密"}' % T0, encoding="utf-8")
        self.assertNotIn("secret", shared.read())

    def test_refreshes_only_when_interval_is_due(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-shared-"))
        shared = _shared(root, interval_s=300)
        first = shared.maybe_refresh(now=T0)
        self.assertEqual(first["at"], round(T0, 3))
        same = shared.maybe_refresh(now=T0 + 120)          # 還沒到 ⇒ 不重採
        self.assertEqual(same["at"], round(T0, 3))
        fresh = shared.maybe_refresh(now=T0 + 400)         # 到了 ⇒ 重採
        self.assertEqual(fresh["at"], round(T0 + 400, 3))

    def test_owner_device_comes_from_the_live_line(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-shared-"))
        threads = _Threads([_Thread(at=T0 - 60, sources={"phone:call"})])
        text = _shared(root, threads=threads).block(now=T0)
        self.assertIn("主人はいまスマホの通話にいる", text)

    def test_public_path_does_not_carry_where_the_owner_is(self):
        """公開筆記那條路：主人在不在、在哪個端是他的行蹤，不能寫進公開產出（2026-11-03 review）。"""
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-shared-"))
        threads = _Threads([_Thread(at=T0 - 60, sources={"phone:call"})])
        shared = _shared(root, threads=threads)
        self.assertIn("主人", shared.block(now=T0))
        public = shared.block(now=T0, public=True)
        self.assertNotIn("主人", public)
        self.assertIn("世界", public, "時間／天氣那一行照給")

    def test_owner_absent_is_stated_as_fact(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-shared-"))
        threads = _Threads([_Thread(at=T0 - 2 * HOUR, sources={"discord"})])
        text = _shared(root, threads=threads).block(now=T0)
        self.assertIn("気配はない", text)
        self.assertIn("2 時間前", text)

    def test_world_block_goes_first_and_is_japanese(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-shared-"))
        threads = _Threads([_Thread(at=T0 - 30, sources={"discord:vc"})])
        shared = _shared(root, threads=threads)
        delta = _delta(root, shared=shared)
        delta.block(now=T0)
        text = delta.block(now=T0 + 60)
        self.assertTrue(text.startswith("［世界："), text)
        self.assertIn("Discord の通話", text)
        self.assertEqual(chinese_hits(text), [], text)

    def test_observe_does_not_move_the_anchor(self):
        """心跳只刷新觀察，不推「距上一歩」的錨點——不然時間感會被刷成 30 秒。"""
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-shared-"))
        delta = _delta(root, shared=_shared(root))
        delta.block(now=T0)
        self.assertEqual(delta._load()["last_at"], round(T0, 3))
        text = delta.observe(now=T0 + HOUR)
        self.assertIn("1 時間", text)
        self.assertEqual(delta._load()["last_at"], round(T0, 3))   # 錨點沒動


class TestGapText(unittest.TestCase):
    def test_units(self):
        self.assertEqual(_gap_text(45), "45 秒")
        self.assertEqual(_gap_text(12 * 60), "12 分")
        self.assertEqual(_gap_text(3 * 3600 + 12 * 60), "3 時間 12 分")
        self.assertEqual(_gap_text(48 * 3600), "2 日")


class TestFirstBlock(unittest.TestCase):
    def test_first_step_says_so_and_uses_her_clock(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-world-"))
        text = _delta(root).block(now=T0)
        self.assertIn("最初の一歩", text)
        self.assertIn("14:20", text)
        self.assertIn("昼", text)

    def test_no_ledger_no_talked_no_notes_only_time_line(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-world-"))
        text = _delta(root, notes_root=None).block(now=T0)
        self.assertNotIn("台帳", text)
        self.assertNotIn("誰も来なかった", text)
        self.assertNotIn("ノート", text)


class TestDiff(unittest.TestCase):
    def test_gap_after_previous_block(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-world-"))
        delta = _delta(root)
        delta.block(now=T0)
        text = delta.block(now=T0 + 3 * HOUR + 12 * 60)
        self.assertIn("前の一歩から 3 時間 12 分", text)

    def test_ledger_reports_delta_then_no_change(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-world-"))
        rows = [{"state": "pending"}, {"state": "pending"}]
        ledger = _Ledger(rows)
        delta = _delta(root, ledger=ledger)
        first = delta.block(now=T0)                  # 第一次：報現狀
        self.assertIn("積まれた 2", first)
        rows.append({"state": "hanging"})            # 多一件續做
        second = delta.block(now=T0 + 60)
        self.assertIn("續きから +1", second)
        third = delta.block(now=T0 + 120)            # 沒變 ⇒ 明說沒變
        self.assertIn("増減なし", third)

    def test_restart_still_knows_gap(self):
        """重啟（新實例）之後，「距上一步多久」照樣算得對。"""
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-world-"))
        _delta(root).block(now=T0)
        text = _delta(root).block(now=T0 + 2 * HOUR)  # 新的物件＝模擬重啟
        self.assertIn("前の一歩から 2 時間", text)


class TestFactsOnly(unittest.TestCase):
    def test_talked_returns_none_when_there_is_no_thread(self):
        """一條活線都沒有 ⇒ **不知道**，不寫「誰も来なかった」（不假裝知道）。"""
        from agent.brain.runtime.inner.world_delta import talked_from_threads

        class _Threads:
            def recent_dialogue(self, entity, limit=40):
                return []

        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-world-"))
        delta = _delta(root, talked=talked_from_threads(_Threads()))
        delta.block(now=T0)
        self.assertNotIn("誰も来なかった", delta.block(now=T0 + HOUR))

    def test_talked_count_and_last_line(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-world-"))
        said = [(2, "今日ひま？")]
        delta = _delta(root, talked=lambda entity, since: said[0])
        delta.block(now=T0)
        text = delta.block(now=T0 + HOUR)
        self.assertIn("声をかけられた：2 回", text)
        self.assertIn("今日ひま？", text)

    def test_nobody_came_is_stated_only_when_known(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-world-"))
        delta = _delta(root, talked=lambda entity, since: (0, ""))
        delta.block(now=T0)
        self.assertIn("誰も来なかった", delta.block(now=T0 + HOUR))

    def test_notes_since_previous_block(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-world-"))
        delta = _delta(root)
        delta.block(now=T0)
        folder = root / "notes" / "mira"
        folder.mkdir(parents=True)
        note = folder / "2026-11-03.md"
        note.write_text("# 日誌\n\n## 15:10 「判定表をまとめる」／整理\nできた\n", encoding="utf-8")
        import os
        os.utime(note, (T0 + 600, T0 + 600))
        text = delta.block(now=T0 + HOUR)
        self.assertIn("ノート：1 件", text)
        self.assertIn("判定表をまとめる", text)

    def test_block_is_japanese(self):
        """進 prompt 的文字一律日語（`tests/prompt_lang.py` 的判據）。"""
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-world-"))
        ledger = _Ledger([{"state": "pending"}, {"state": "completed"}])
        delta = _delta(root, ledger=ledger, talked=lambda e, s: (1, "テスト"))
        delta.block(now=T0)
        text = delta.block(now=T0 + HOUR)
        self.assertEqual(chinese_hits(text), [], text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
