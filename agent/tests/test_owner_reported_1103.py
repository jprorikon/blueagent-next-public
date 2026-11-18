#!/usr/bin/env python3
"""owner 2026-11-03 抓到的三件事的回歸測試。

① **〔私記〕不得出現在回覆裡**：nori 實測把內部標記照抄進正文 4 次
   （「我记得，〔私記〕ノリ「…」3/4…」）。
② **同一個 preset 不要在看板上堆疊**：活動輪換回到同一個 preset 時，若還有 pending／
   processing／hanging，就回收那一筆；已完成的是歷史，照樣各留一筆。
   另外 `processing` 但活動已不存在的「殭屍」要在開機對帳時退回 hanging。
③ **她自己發出去的主动消息要進她的 transcript**：否則主人回「見る」時她回
   「何を見る？」——她不知道自己剛剛說了什麼。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))   # 倉庫根

from agent.brain.compose.proactive_echo import make_proactive_recorder  # noqa: E402
from agent.brain.domain.thread import Thread, TurnRecord  # noqa: E402
from agent.brain.runtime.inner.task_ledger import TaskLedger  # noqa: E402
from agent.brain.runtime.text_clean import clean_reply  # noqa: E402
from prompt_lang import chinese_hits  # noqa: E402


class TestPrivateMarkerNeverLeaks(unittest.TestCase):
    def test_marker_is_stripped(self):
        raw = "我记得，〔私記〕ノリ「学んだゲームの知識を復習して」3/4 ノートと照らして…"
        out = clean_reply(raw)
        self.assertNotIn("〔私記〕", out)
        self.assertIn("学んだゲームの知識", out)      # 內容照留，只去標記

    def test_normal_reply_untouched(self):
        for text in ("おかえりなさい。……猫缶、重くなかったのですか。",
                     "これはうん（真面目に）"):
            with self.subTest(text=text):
                self.assertEqual(clean_reply(text), text)

    def test_strip_leaves_no_double_space(self):
        self.assertEqual(clean_reply("〔私記〕  今日は静かだった"), "今日は静かだった")


class TestLedgerDedupe(unittest.TestCase):
    def _ledger(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-ledger-"))
        return TaskLedger(root, "nori")

    def test_same_preset_reuses_the_open_task(self):
        ledger = self._ledger()
        first = ledger.add("ストアを見る", preset="store", state="pending")
        again = ledger.add("ストアを見る", preset="store", state="pending")
        self.assertEqual(first["id"], again["id"], "同一個 preset 還有 pending ⇒ 應該回收那一筆")
        self.assertEqual(len(ledger.items()), 1)

    def test_completed_history_is_kept(self):
        ledger = self._ledger()
        first = ledger.add("表にする", preset="table", state="pending")
        ledger.set_state(first["id"], "completed")
        second = ledger.add("表にする", preset="table", state="pending")
        self.assertNotEqual(first["id"], second["id"], "已完成的是歷史，要留一筆新的")
        self.assertEqual(len(ledger.items()), 2)

    def test_no_preset_never_dedupes(self):
        ledger = self._ledger()
        a = ledger.add("手動指定", preset="", state="pending")
        b = ledger.add("手動指定", preset="", state="pending")
        self.assertNotEqual(a["id"], b["id"])

    def test_reconcile_reclaims_zombie_processing(self):
        ledger = self._ledger()
        alive = ledger.add("まだ生きている", preset="alive", state="processing")
        dead = ledger.add("もう無い", preset="dead", state="processing")
        ledger.patch(alive["id"], activity_id="act-alive")
        ledger.patch(dead["id"], activity_id="act-gone")
        fixed = ledger.reconcile(["act-alive"])
        self.assertEqual(fixed, 1)
        self.assertEqual(ledger.get(alive["id"])["state"], "processing")
        self.assertEqual(ledger.get(dead["id"])["state"], "hanging")

    def test_live_activity_task_survives_reconcile(self):
        """正在跑的活動開工時就該記 activity_id；舊資料靠活動身上的 task_id 認（2026-11-03 review 實測誤回收）。"""
        from agent.brain.domain.activity import Activity
        ledger = self._ledger()
        live = Activity.create("nori", "ストアページを見る", ["a", "b"])
        live.meta["preset"] = "game-shop"
        ledger.on_start(live)
        task_id = live.meta["task_id"]
        self.assertEqual(ledger.get(task_id)["activity_id"], str(live.id))
        self.assertEqual(ledger.reconcile([str(live.id)]), 0)
        self.assertEqual(ledger.get(task_id)["state"], "processing")
        # 舊資料：processing 但 activity_id 空著 ⇒ 用 task_id 認得出是活的
        ledger.patch(task_id, activity_id="")
        self.assertEqual(ledger.reconcile([], [task_id]), 0)
        self.assertEqual(ledger.get(task_id)["state"], "processing")

    def test_start_reusing_an_open_task_marks_it_processing(self):
        """`add()` 回收了同 preset 的 pending ⇒ 開工時也要變 processing、嘗試次數 +1。"""
        from agent.brain.domain.activity import Activity
        ledger = self._ledger()
        pending = ledger.add("集めた数字を表にする", preset="machine-chart")
        act = Activity.create("nori", "集めた数字を表にする", ["a"])
        act.meta["preset"] = "machine-chart"
        ledger.on_start(act)
        self.assertEqual(act.meta["task_id"], pending["id"], "同 preset 沒做完的那筆被回收")
        row = ledger.get(pending["id"])
        self.assertEqual(row["state"], "processing")
        self.assertEqual(row["attempts"], 1)


class _Threads:
    def __init__(self, rows):
        self.rows = list(rows)

    def threads(self):
        return tuple(self.rows)


class TestProactiveEcho(unittest.TestCase):
    def _thread(self, *, entity="mira", people=("mira", "owner"), at=1000.0):
        from agent.brain.domain.ids import ThreadKey
        from agent.brain.domain.thread import SourceClass
        return Thread(key=ThreadKey(scope="owner", entity=entity), id=f"th_{entity}-1",
                      lane=SourceClass.INTERACTIVE, created_at=at - 10.0, last_active_at=at,
                      participants=set(people))

    def test_message_lands_in_her_transcript(self):
        thread = self._thread()
        record = make_proactive_recorder(_Threads([thread]))
        self.assertTrue(record("mira", "owner", "成果物、見てくれる？", 2000.0))
        self.assertEqual(len(thread.transcript), 1)
        turn = thread.transcript[0]
        self.assertEqual(turn.role, "assistant")
        self.assertEqual(turn.text, "成果物、見てくれる？")
        self.assertEqual(turn.source, "proactive")

    def test_picks_the_thread_with_the_owner(self):
        with_owner = self._thread(people=("mira", "owner"), at=1500.0)
        guest = self._thread(people=("mira", "guest-x"), at=1900.0)
        record = make_proactive_recorder(_Threads([guest, with_owner]))
        self.assertTrue(record("mira", "owner", "よるの話", 2000.0))
        self.assertEqual(len(with_owner.transcript), 1)
        self.assertEqual(len(guest.transcript), 0)

    def test_private_message_never_lands_in_a_group_thread(self):
        """主動消息是私下發給他的：群聊線（他在、還有別人）再新也不寫進去（2026-11-03 review 實測挑中群聊線）。"""
        from agent.brain.domain.ids import ThreadKey
        from agent.brain.domain.thread import SourceClass
        private = self._thread(people=("mira", "owner"), at=1500.0)
        group = Thread(key=ThreadKey(scope="group:discord-1", entity="mira"), id="th_group-1",
                       lane=SourceClass.INTERACTIVE, created_at=1800.0, last_active_at=1990.0,
                       participants={"mira", "owner", "alice", "bob"})
        record = make_proactive_recorder(_Threads([group, private]))
        self.assertTrue(record("mira", "owner", "オーナー、成果物見てくれる？", 2000.0))
        self.assertEqual(len(private.transcript), 1)
        self.assertEqual(len(group.transcript), 0, "群聊線不能收到只發給主人的那句")
        only_group = make_proactive_recorder(_Threads([group]))
        self.assertFalse(only_group("mira", "owner", "ひとりごと", 2100.0), "沒有私聊線就不記")

    def test_no_matching_thread_is_not_fabricated(self):
        record = make_proactive_recorder(_Threads([]))
        self.assertFalse(record("mira", "owner", "ひとりごと", 2000.0))

    def test_next_owner_turn_sees_it(self):
        """主人下一句進來時，她的上下文裡就有那句（這是這個修正的目的）。"""
        thread = self._thread()
        record = make_proactive_recorder(_Threads([thread]))
        record("mira", "owner", "成果物、見てくれる？", 2000.0)
        thread.record(TurnRecord(turn_id="t_next", role="user", text="見る",
                                 source="phone:owner", at=2001.0, speaker="owner"))
        spoken = [turn for turn in thread.transcript if turn.role == "assistant"]
        self.assertEqual(len(spoken), 1)
        self.assertIn("成果物", spoken[0].text)
        self.assertEqual(spoken[0].text, "成果物、見てくれる？")


if __name__ == "__main__":
    unittest.main(verbosity=2)
