#!/usr/bin/env python3
"""存摺（RP 帳本）：**只記帳、不擋人**（owner 2026-11-01 第③項）。

本檔釘住四件事：
  1. `rp_ledger.selftest()` **12 項全過**（同格冪等／可透支／src 冪等／更正只准指 deposit·spend／
     崩尾搬 .torn／中間壞行不炸／併發序號不撞）—— 真跑一遍，不是宣稱。
  2. 鏡射（`LedgerMirror`）：預算是**閘**、存摺是**帳**；閘的判斷一個字不改，帳多記一筆。
  3. 匯率照 aster：0.1 RP／5 分鐘；單位 1 點＝1 RP；**不設到期**。
  4. `status()` 是唯一權威：`balance_rp` ＝ 流水逐筆相加，不是另一份被維護的數字。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agent.brain.runtime.inner import rp_ledger as rp  # noqa: E402


class Clock:
    def __init__(self, start: float = 5000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeBudget:
    """假的閘：只記下被叫了什麼（真正的判斷不在這個測試的範圍）。"""

    def __init__(self) -> None:
        self.spent: list[tuple[str, float]] = []
        self.accrued: list[str] = []

    def accrue(self, entity: str) -> dict:
        self.accrued.append(entity)
        return {"balance": 1.0}

    def try_spend(self, entity: str, cost: float, *, reason: str = "", **kwargs) -> bool:
        self.spent.append((entity, cost))
        return True

    def charge(self, entity: str, cost: float, *, reason: str = "", **kwargs) -> dict:
        self.spent.append((entity, cost))
        return {"ok": True}

    def mood(self, entity: str) -> dict:
        return {"balance": 3.0}


class TestRpLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="rp-test-")
        self.clock = Clock()
        self.book = rp.RpLedger(self.root, clock=self.clock)

    # 1 ── 12 項自測（真的在 selftest 裡跑）
    def test_selftest_passes(self) -> None:
        result = rp.selftest()
        self.assertEqual(result["failed"], 0,
                         [name for name, ok in result["checks"] if not ok])
        self.assertGreaterEqual(result["passed"], 12)

    # 2 ── 匯率：0.1 RP／5 分鐘，且同一格只入一次
    def test_rate_and_grid_idempotency(self) -> None:
        self.book.deposit_tick("mira")
        self.clock.tick(30)
        again = self.book.deposit_tick("mira")
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(self.book.status("mira")["balance_rp"], 0.1)
        self.clock.tick(300)
        self.book.deposit_tick("mira")
        self.assertEqual(self.book.status("mira")["balance_rp"], 0.2)
        self.assertEqual(rp.RATE_MRP, 100, "匯率照 aster：0.1 RP／5 分鐘")

    # 3 ── 不設到期：放著不會消失（owner 2026-11-01 第②條）
    def test_no_expiry(self) -> None:
        self.book.deposit("mira", 5000, src="gift:1")
        self.clock.tick(86400 * 30)                      # 一個月後
        self.assertEqual(self.book.status("mira")["balance_rp"], 5.0,
                         "我們不做到期，餘額不會蒸發")

    # 4 ── 鏡射：閘照舊、帳多一筆
    def test_mirror_books_but_does_not_gate(self) -> None:
        gate = FakeBudget()
        mirror = rp.LedgerMirror(gate, self.book, clock=self.clock)
        mirror.accrue("mira")
        self.assertEqual(gate.accrued, ["mira"], "閘照樣被叫")
        self.assertEqual(self.book.status("mira")["balance_rp"], 0.1, "帳多記一筆入帳")
        self.assertTrue(mirror.try_spend("mira", 1.0, reason="study"))
        self.assertEqual(gate.spent, [("mira", 1.0)], "閘照樣收到扣款請求")
        self.assertEqual(self.book.status("mira")["balance_rp"], -0.9, "帳也記了（可以負）")
        self.assertEqual(mirror.mood("mira")["balance"], 3.0, "其餘介面照舊轉給原物件")

    # 5 ── 唯一權威：改了流水，balance 立刻跟著（沒有第二份數字）
    def test_status_is_derived_not_cached(self) -> None:
        self.book.deposit("mira", 1000, src="gift:1")
        before = self.book.status("mira")["balance_rp"]
        path = self.book.path("mira")
        with path.open("a", encoding="utf-8") as stream:
            import json
            stream.write(json.dumps({"seq": 9, "at": 1.0, "kind": "spend",
                                     "amount_mrp": -300, "src": "manual:x", "note": "手動補記"}) + "\n")
        after = self.book.status("mira")["balance_rp"]
        self.assertEqual(round(before - after, 3), 0.3, "balance 是從流水算的，不是被維護的數字")


if __name__ == "__main__":
    unittest.main(verbosity=2)
