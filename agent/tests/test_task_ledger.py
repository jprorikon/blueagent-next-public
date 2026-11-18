#!/usr/bin/env python3
"""任務台帳（owner 2026-11-03）：她自己得有一套任務狀態管理。

六個狀態：pending／processing／hanging／locked／outdated／completed。
判據：
  ① **先查再動手**：`next_task()` 先撿 hanging（被打斷的），再撿 pending（優先度高的）；
  ② `locked` 有 `wait_until`，時間到了自動解鎖回 pending；沒到就不撿；
  ③ 放太久／失敗太多次 ⇒ `outdated`（不再撿）；
  ④ `completed` 永久留著（她想回想「我做過什麼」靠它）；
  ⑤ 被打斷 ⇒ hanging 並記下 cursor／note（續做時知道從哪接）。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.domain.activity import Activity  # noqa: E402
from agent.brain.runtime.inner.task_ledger import STATES, TaskLedger  # noqa: E402


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestStates(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-ledger-"))
        self.clock = _Clock()
        self.ledger = TaskLedger(self.root, "mira", clock=self.clock)

    def test_six_states_are_declared(self):
        self.assertEqual(STATES, ("pending", "processing", "hanging", "locked",
                                  "outdated", "completed"))

    def test_new_task_is_pending_and_picked(self):
        task = self.ledger.add("ノートを読み返す", preset="notes-reread")
        self.assertEqual(task["state"], "pending")
        picked = self.ledger.next_task()
        self.assertEqual(picked["id"], task["id"])

    def test_lifecycle_processing_hanging_completed(self):
        task = self.ledger.add("判定表をまとめる", preset="trpg-table")
        activity = Activity.create("mira", "判定表をまとめる", ["a", "b", "c"])
        activity.meta["task_id"] = task["id"]
        activity.cursor = 2
        self.ledger.on_start(activity)
        self.assertEqual(self.ledger.get(task["id"])["state"], "processing")
        self.ledger.on_interrupt(activity)
        hanging = self.ledger.get(task["id"])
        self.assertEqual(hanging["state"], "hanging")
        self.assertEqual(hanging["cursor"], 2)
        self.assertEqual(hanging["steps"], 3, "看板要寫「第 n／total 步起」")
        row = self.ledger.board()["columns"]["hanging"][0]
        self.assertEqual((row["cursor"], row["steps"]), (2, 3))
        # 被打斷的優先被撿回來
        self.assertEqual(self.ledger.next_task()["id"], task["id"])
        self.ledger.on_done(activity)
        self.assertEqual(self.ledger.get(task["id"])["state"], "completed")
        self.assertIsNone(self.ledger.next_task())          # 手上沒事了

    def test_hanging_beats_pending(self):
        pending = self.ledger.add("あとでやる", priority=9.0)
        hanging = self.ledger.add("中断したやつ")
        self.ledger.set_state(hanging["id"], "hanging")
        picked = self.ledger.next_task()
        self.assertEqual(picked["id"], hanging["id"])       # 先續做，再排新的
        self.assertNotEqual(picked["id"], pending["id"])

    def test_locked_waits_until_due(self):
        task = self.ledger.add("返事待ち", note="相手の返事を待っている")
        self.ledger.set_state(task["id"], "locked", wait_until=self.clock.now + 600)
        self.assertIsNone(self.ledger.next_task())          # 條件沒到 ⇒ 不撿
        self.clock.now += 700
        self.assertEqual(self.ledger.next_task()["id"], task["id"])

    def test_stale_and_repeated_failures_become_outdated(self):
        old = self.ledger.add("ずっと放置していた件")
        self.ledger.patch(old["id"], updated_at=self.clock.now - 20 * 86400)
        failed = self.ledger.add("何度も失敗する件")
        self.ledger.patch(failed["id"], attempts=3)
        self.assertIsNone(self.ledger.next_task())
        self.assertEqual(self.ledger.get(old["id"])["state"], "outdated")
        self.assertEqual(self.ledger.get(failed["id"])["state"], "outdated")

    def test_summary_shows_backlog(self):
        hanging = self.ledger.add("中断したノート作業")
        self.ledger.set_state(hanging["id"], "hanging")
        locked = self.ledger.add("返事待ち")
        self.ledger.set_state(locked["id"], "locked", wait_until=self.clock.now + 60)
        self.ledger.add("これからやる")
        board = self.ledger.summary()
        self.assertIn("タスク台帳", board)
        self.assertIn("hanging", board)
        self.assertIn("中断したノート作業", board)
        self.assertIn("条件待ち", board)

    def test_state_survives_reload(self):
        task = self.ledger.add("再起動しても残る")
        self.ledger.set_state(task["id"], "hanging")
        again = TaskLedger(self.root, "mira", clock=self.clock)
        self.assertEqual(again.get(task["id"])["state"], "hanging")

    def test_due_task_is_picked_first_but_locked_is_not(self):
        """預定時刻到了就先做（她自己約的事）；但 locked 是「等條件」，時刻到了也不能先開工。"""
        self.ledger.add("ふつうの続き", priority=9.0)
        waiting = self.ledger.add("返事待ち", due_at=self.clock.now - 60)
        self.ledger.set_state(waiting["id"], "locked", wait_until=self.clock.now + 3600)
        due = self.ledger.add("約束の時間", due_at=self.clock.now - 30)
        self.assertEqual([row["id"] for row in self.ledger.due_tasks()], [due["id"]])
        self.assertEqual(self.ledger.next_task()["id"], due["id"])
        self.ledger.set_state(due["id"], "completed")
        picked = self.ledger.next_task()
        self.assertNotEqual(picked["id"], waiting["id"], "條件還沒到的 locked 不能因為到期就開工")

    def test_due_tasks_sees_what_mcp_just_added(self):
        """`due_tasks` 也會被生活線的醒來提示直接叫（不經 next_task）⇒ 要讀得到另一個行程剛排的。"""
        other = TaskLedger(self.root, "mira", clock=self.clock)
        added = other.add("MCP で入れた予定", due_at=self.clock.now - 1)
        self.assertEqual([row["id"] for row in self.ledger.due_tasks()], [added["id"]])

    def test_wake_hint_only_counts_startable_tasks(self):
        """醒來提示：只看這本台帳、只算還能開工的（locked 不該每 60 秒把她叫醒）。"""
        import time as _time
        from agent.brain.compose.life_wiring import _wake_hint
        real = TaskLedger(self.root, "mira")                 # 醒來提示用真時鐘
        locked = real.add("返事待ち", due_at=_time.time() - 60)
        real.set_state(locked["id"], "locked", wait_until=_time.time() + 3600)
        self.assertEqual(_wake_hint(real, 1800.0)(), 1800.0, "只有 locked ⇒ 照常睡滿")
        real.add("30 分後の約束", due_at=_time.time() + 600)
        self.assertLess(_wake_hint(real, 1800.0)(), 700.0, "排好的事快到了 ⇒ 提早醒")
        self.assertEqual(_wake_hint(None, 1800.0)(), 1800.0)

    def test_other_writer_is_seen_and_not_overwritten(self):
        """應用的常駐實例 ＋ MCP server（另一個行程、另一份實例）寫同一份檔。

        以前常駐那份只在啟動時讀一次：MCP 新增的看不到，自己一存檔就把它蓋掉。
        """
        mine = self.ledger.add("アプリ側で積んだ")
        other = TaskLedger(self.root, "mira", clock=self.clock)   # ＝MCP 那一側
        theirs = other.add("MCP で積んだ")
        self.assertIsNotNone(self.ledger.get(theirs["id"]), "對方新增的要看得到")
        self.ledger.comment(mine["id"], "これは急がなくていい")       # 常駐那份寫一次
        again = TaskLedger(self.root, "mira", clock=self.clock)
        self.assertIsNotNone(again.get(theirs["id"]), "常駐那份存檔不能蓋掉對方的新增")
        self.assertEqual(len(again.get(mine["id"])["comments"]), 1)
        other.set_state(mine["id"], "completed")
        self.assertEqual(self.ledger.board()["counts"]["completed"], 1, "看板要反映對方的改動")


if __name__ == "__main__":
    unittest.main(verbosity=2)
