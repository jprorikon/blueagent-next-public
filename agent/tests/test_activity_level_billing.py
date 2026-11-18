#!/usr/bin/env python3
"""計價單位：**一次活動（回合）收一次**，外網明細併成一筆（owner 2026-11-01 照 aster 的設計改）。

aster 2026-11-01 的原話：「我的 1 RP ＝ 一次模型呼叫」；「你的『一步』如果 1 RP，你買到的東西
大概等於我的 1/3 到 1/9——差距不在價格，在**單位**」；外網「不要記四筆，記一筆帶明細」。

改之前：`LifeLoop` 每一步都 `try_spend(一件的價)` ⇒ 一件 5 步的事被收 5 倍；一步裡三次外網
再各記一筆 ⇒ 帳面 4 筆，看不出「這一步很貴」。
改之後：**一件只收一次**（`src=activity:<id>` 讓存摺冪等），外網累積到 `meta`，
在這件活動結束時**併成一筆**（`src=activity:<id>:net`）。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agent.brain.runtime.life_loop import LifeLoop  # noqa: E402
from agent.brain.runtime.life_step import _charge_network  # noqa: E402


class FakeBudget:
    def __init__(self) -> None:
        self.spent: list[dict] = []
        self.charged: list[dict] = []

    def try_spend(self, entity, cost, *, reason="", src=""):
        self.spent.append({"entity": entity, "cost": cost, "reason": reason, "src": src})
        return True

    def charge(self, entity, cost, *, reason="", src=""):
        self.charged.append({"entity": entity, "cost": cost, "reason": reason, "src": src})
        return cost


class FakeActivity:
    def __init__(self, ident: str = "act-1", kind: str = "study") -> None:
        self.id = ident
        self.kind = kind
        self.title = "少し調べる"
        self.meta: dict = {}


def make_loop(budget, cost: float = 1.0):
    """只裝這幾個欄位的最小 LifeLoop（不啟執行緒；要測的是記帳語意，不是排程）。"""
    loop = object.__new__(LifeLoop)
    loop.entity = "mira"
    loop._budget = budget
    loop._log = lambda _message: None
    loop._cost_of = lambda _activity: cost
    return loop


class TestActivityLevelBilling(unittest.TestCase):
    def test_one_charge_per_activity(self) -> None:
        budget = FakeBudget()
        loop = make_loop(budget)
        activity = FakeActivity()
        for _ in range(5):                      # 五步
            loop._charge_activity_once(activity)
        self.assertEqual(len(budget.spent), 1, "一件活動只該收一次（不是每步一次）")
        self.assertEqual(budget.spent[0]["src"], "activity:act-1", "冪等鍵帶活動 id")

    def test_network_accumulates_then_settles_once(self) -> None:
        budget = FakeBudget()
        loop = make_loop(budget)
        activity = FakeActivity()
        tools = [{"name": "mcp_firecrawl_firecrawl_search"}, {"name": "mcp_firecrawl_firecrawl_search"}, {"name": "mcp_firecrawl_firecrawl_scrape"}]
        for _ in range(3):                      # 三步各打一次外網
            _charge_network(activity, tools, "調べる", budget=budget, net_cost=1.5)
        self.assertEqual(budget.charged, [], "累積期間不該當場扣錢")
        self.assertEqual(activity.meta["net_points"], 13.5)
        self.assertEqual(activity.meta["net_hits"], 9)
        loop._settle_network(activity)
        loop._settle_network(activity)          # 重複叫也不該再記
        self.assertEqual(len(budget.charged), 1, "外網明細只併成一筆")
        self.assertEqual(budget.charged[0]["cost"], 13.5)
        self.assertIn("外網 ×9", budget.charged[0]["reason"])
        self.assertEqual(budget.charged[0]["src"], "activity:act-1:net")

    def test_no_network_no_entry(self) -> None:
        budget = FakeBudget()
        loop = make_loop(budget)
        activity = FakeActivity()
        _charge_network(activity, [{"name": "board.feed"}], "整理", budget=budget, net_cost=1.5)
        loop._settle_network(activity)
        self.assertEqual(budget.charged, [], "沒打外網就不該多一筆")

    def test_src_keeps_ledger_idempotent_across_restart(self) -> None:
        """同一件活動重入（重啟後接著做）不會被記第二次。"""
        import tempfile

        from agent.brain.runtime.inner.rp_ledger import RpLedger

        book = RpLedger(tempfile.mkdtemp(prefix="rp-bill-"))
        budget = FakeBudget()

        class Mirror:
            def __init__(self, ledger):
                self._ledger = ledger

            def try_spend(self, entity, cost, *, reason="", src=""):
                self._ledger.spend(entity, cost, src=src, note=reason)
                return True

        loop = make_loop(Mirror(book))
        activity = FakeActivity()
        loop._charge_activity_once(activity)
        activity.meta.pop("charged", None)     # 模擬重啟後 meta 掉了
        loop._charge_activity_once(activity)
        self.assertEqual(book.status("mira")["entries"], 1, "存摺靠 src 冪等，重入不會重記")


if __name__ == "__main__":
    unittest.main(verbosity=2)
