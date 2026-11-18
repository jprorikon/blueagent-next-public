#!/usr/bin/env python3
"""主人的兩種介入方式（owner 2026-11-03）：

  ① **批注**：主人在 App 上對某條任務留一句話 ⇒ 她**下次檢查**時看到（`unseen_comments`），
     怎麼處理由她自己判斷（改狀態／改日期／回話），處理完 `ack` 標記看過；
  ② **直接對話要求改**：他跟她說，她用自己的 MCP 工具改——這條不需要額外機制。

判據：
  · 批注**不改任務狀態**（`state` 只有她自己能改）；
  · 未讀批注會出現在台帳摘要（她 prompt 裡那段）與 `board()` 的 `unseen_comments`；
  · `ack` 之後不再算未讀，但批注本身永久留著（她可以回頭看主人說過什麼）。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.runtime.inner.task_ledger import TaskLedger  # noqa: E402


class TestOwnerComments(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-comments-"))
        self.ledger = TaskLedger(self.root, "mira")
        self.task = self.ledger.add("判定表をまとめる", preset="trpg-table")

    def test_comment_does_not_change_state(self):
        before = self.ledger.get(self.task["id"])["state"]
        self.ledger.comment(self.task["id"], "この件は今週中に頼むよ")
        after = self.ledger.get(self.task["id"])
        self.assertEqual(after["state"], before)
        self.assertEqual(after["comments"][0]["text"], "この件は今週中に頼むよ")
        self.assertEqual(after["comments"][0]["seen_at"], 0.0)

    def test_unseen_then_ack(self):
        self.ledger.comment(self.task["id"], "明日でいい")
        unseen = self.ledger.unseen_comments()
        self.assertEqual(len(unseen), 1)
        self.assertEqual(unseen[0]["id"], self.task["id"])
        self.assertEqual(self.ledger.ack_comments(), 1)
        self.assertEqual(self.ledger.unseen_comments(), [])
        # 批注本身留著（她可以回頭看主人說過什麼）
        self.assertEqual(len(self.ledger.get(self.task["id"])["comments"]), 1)

    def test_board_surfaces_unseen_counts(self):
        self.ledger.comment(self.task["id"], "今日中に")
        board = self.ledger.board()
        self.assertEqual(board["unseen_comments"], 1)
        row = board["columns"]["pending"][0]
        self.assertEqual(row["unseen_comments"], 1)
        self.assertEqual(row["comments"][0]["text"], "今日中に")
        self.assertFalse(row["comments"][0]["seen"])

    def test_summary_mentions_comments(self):
        self.ledger.comment(self.task["id"], "これは来週でいい")
        board_text = self.ledger.summary()
        self.assertIn("批注", board_text)
        self.assertIn("これは来週でいい", board_text)

    def test_comment_on_unknown_task(self):
        self.assertIsNone(self.ledger.comment("task_nope", "…"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
