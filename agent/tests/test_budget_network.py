#!/usr/bin/env python3
"""網路費（owner 2026-10-25 定價；2026-11-01 改成「一步一筆明細」）。

判據（改後，照 aster 2026-11-01 的意見）：
  ① 一步真的調了 `web.*` ⇒ 按次數**累積**到 `activity.meta`（失敗的呼叫也算：請求已經花掉了）；
  ② 這件活動結束時**併成一筆**收（`LifeLoop._settle_network`），不再每步各記一筆 ——
     aster：「一步 1 RP ＋ 同一步三次外網 4.5 ⇒ 帳面看不出『這步很貴』，只看到四筆扣款」；
  ③ 沒碰到網路（`notes.read` 這類本機工具）⇒ 一毛都不多扣；
  ④ 價格沒設（0）或沒接 budget ⇒ 完全照舊。
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.domain.activity import Activity  # noqa: E402
from agent.brain.domain.entity import CharacterEntity, EntityRegistry  # noqa: E402
from agent.brain.runtime.life import NET_TOOLS, make_step_fn  # noqa: E402
from agent.brain.runtime.life_loop import LifeLoop  # noqa: E402


def settle(activity, budget) -> None:
    """把「外網明細」結成那一筆（真實路徑在 `LifeLoop._run`／失敗分支）。"""
    loop = object.__new__(LifeLoop)
    loop.entity = "mira"
    loop._budget = budget
    loop._log = lambda _message: None
    loop._settle_network(activity)


class FakeBudget:
    """只記帳的替身：看得出「扣了幾次、總共多少」。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.debts: list[tuple[str, float]] = []

    def try_spend(self, entity: str, cost: float, *, reason: str = "") -> bool:
        self.calls.append((entity, float(cost)))
        return True

    def charge(self, entity: str, cost: float, *, reason: str = "", src: str = "") -> float:
        self.debts.append((entity, float(cost)))
        return float(cost)

    @property
    def total(self) -> float:
        return sum(cost for _, cost in self.calls) + sum(cost for _, cost in self.debts)


class FakeMemory:
    def add(self, *args, **kwargs):
        return None


def registry() -> EntityRegistry:
    store = EntityRegistry()
    store.register(CharacterEntity(id="mira", name="ミラ", soul="mira"), replace=True)
    return store


def activity(kind: str = "play") -> Activity:
    return Activity.create("mira", "逛商店頁", ["查這週有什麼新作"], kind=kind)


def step_fn_with(trace: list[dict], *, budget=None, net_cost: float | None = None,
                 ok: bool = True, call=None, memory=None):
    def work(title, text, **kwargs):
        return {"ok": ok, "text": "查到三款遊戲，挑了節奏最慢的那款。" if ok else "查不到",
                "trace": trace, "reason": "" if ok else "no_results"}

    return make_step_fn(memory or FakeMemory(), registry(), 0.0, work=work, budget=budget,
                        net_cost=net_cost, log=None, call=call)


class RecordingMemory:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def add(self, text, *args, **kwargs):
        self.texts.append(str(text))


class TestNetworkCost(unittest.TestCase):
    def test_web_calls_accumulate_and_bill_once(self):
        budget = FakeBudget()
        write = step_fn_with([{"name": "mcp_firecrawl_firecrawl_search", "ok": True},
                              {"name": "mcp_firecrawl_firecrawl_scrape", "ok": False},
                              {"name": "notes.read", "ok": True}],
                             budget=budget, net_cost=1.5)
        act = activity()
        write(act, act.steps[0])
        self.assertEqual(budget.debts, [], "這一步不當場扣錢")
        self.assertEqual(act.meta["net_hits"], 2, "兩次網路呼叫（失敗那次也算）")
        self.assertAlmostEqual(act.meta["net_points"], 3.0, msg="1.5 × 2")
        settle(act, budget)
        self.assertEqual(len(budget.debts), 1, "整件活動只留一筆網路明細")
        self.assertAlmostEqual(budget.total, 3.0)

    def test_failed_work_still_pays_for_requests_already_sent(self):
        """★ review.md 2026-10-26 第 2 條：整個 work 回 ok=False（搜尋沒結果、工具全失敗），
        已經打出去的網路請求照樣計費——以前失敗路徑回空 trace，查不到就永遠不扣費。"""
        trace = [{"name": "mcp_firecrawl_firecrawl_search", "ok": False, "error": "no_results"},
                 {"name": "mcp_firecrawl_firecrawl_scrape", "ok": True}]
        for call in (None, lambda prompt: "憑印象寫了一段。"):
            budget = FakeBudget()
            write = step_fn_with(trace, budget=budget, net_cost=1.5, ok=False, call=call)
            act = activity()
            write(act, act.steps[0])
            settle(act, budget)
            self.assertAlmostEqual(budget.total, 3.0, msg=f"call={call}")

    def test_failed_work_is_not_stamped_as_verified(self):
        """計費看「叫過什麼」，查證章看「成功查到什麼」：失敗的查詢不能讓筆記變成「查證過」。"""
        memory = RecordingMemory()
        write = step_fn_with([{"name": "mcp_firecrawl_firecrawl_search", "ok": False, "error": "no_results"}],
                             budget=FakeBudget(), net_cost=1.5, ok=False,
                             call=lambda prompt: "憑印象寫了一段。", memory=memory)
        act = activity()
        write(act, act.steps[0])
        self.assertTrue(any("未検証" in text for text in memory.texts), memory.texts)

    def test_calls_that_were_never_sent_are_free(self):
        budget = FakeBudget()
        write = step_fn_with([{"name": "mcp_firecrawl_firecrawl_search", "ok": False, "error": "no_context"},
                              {"name": "mcp_firecrawl_firecrawl_scrape", "ok": False,
                               "error": "not_in_self_work_whitelist"}],
                             budget=budget, net_cost=1.5)
        act = activity()
        write(act, act.steps[0])
        self.assertEqual(budget.total, 0.0)

    def test_calls_blocked_by_step_budget_are_free(self):
        """步內預算／重複呼叫擋下來的 `web.*` 根本沒送出去 ⇒ 不收網路費（以前照收，越亂叫扣越多）。"""
        budget = FakeBudget()
        write = step_fn_with([{"name": "mcp_firecrawl_firecrawl_search", "ok": True},
                              {"name": "mcp_firecrawl_firecrawl_search", "ok": False, "error": "step_call_budget"},
                              {"name": "mcp_firecrawl_firecrawl_scrape", "ok": False, "error": "duplicate_in_step"}],
                             budget=budget, net_cost=1.5)
        act = activity()
        write(act, act.steps[0])
        self.assertEqual(act.meta["net_hits"], 1, "擋下來的兩次不算")
        settle(act, budget)
        self.assertAlmostEqual(budget.total, 1.5, msg="只有真的送出去的那一次")

    def test_local_tools_are_free(self):
        budget = FakeBudget()
        write = step_fn_with([{"name": "notes.read", "ok": True}], budget=budget, net_cost=1.5)
        write(activity(), None)
        self.assertEqual(budget.calls, [])

    def test_price_zero_keeps_old_behaviour(self):
        budget = FakeBudget()
        write = step_fn_with([{"name": "mcp_firecrawl_firecrawl_search", "ok": True}], budget=budget, net_cost=0.0)
        act = activity()
        write(act, act.steps[0])
        settle(act, budget)
        self.assertEqual(budget.calls, [])

    def test_price_can_come_from_env_and_vm_browser_is_billed(self):
        budget = FakeBudget()
        write = step_fn_with([{"name": "terminal", "ok": True,
                               "arguments": {"cmd": "web text https://example.org"}}],
                             budget=budget)
        os.environ["BLUEAGENT_BUDGET_NET_COST"] = "2"
        act = activity()
        try:
            write(act, act.steps[0])
        finally:
            os.environ.pop("BLUEAGENT_BUDGET_NET_COST", None)
        settle(act, budget)
        self.assertAlmostEqual(budget.total, 2.0)

    def test_local_terminal_command_is_not_billed_as_network(self):
        budget = FakeBudget()
        write = step_fn_with([{"name": "terminal", "ok": True,
                               "arguments": {"cmd": "pwd"}},
                              {"name": "terminal", "ok": True,
                               "arguments": {"cmd": "python3 web-counter.py"}}],
                             budget=budget, net_cost=1.5)
        act = activity()
        write(act, act.steps[0])
        self.assertNotIn("net_hits", act.meta)

    def test_net_tools_are_the_outbound_ones(self):
        # 2026-11-08：發帖（mcp_board_post）也是「打出去的行動」，按一次外網計價
        self.assertEqual(set(NET_TOOLS), {"mcp_board_post",
                                          "mcp_firecrawl_firecrawl_search",
                                          "mcp_firecrawl_firecrawl_scrape",
                                          "mcp_firecrawl_firecrawl_map",
                                          "mcp_firecrawl_firecrawl_crawl"})


class TestBudgetDebt(unittest.TestCase):
    """`charge` 一定要落到帳上（可以欠）：不然餘額不夠時網路費會整筆消失。"""

    def test_charge_can_go_negative_and_blocks_then_recovers(self):
        import tempfile

        from agent.brain.runtime.budget import ActionBudget

        root = tempfile.mkdtemp(prefix="ba-budget-")
        budget = ActionBudget(root, start=1.0, cap=24.0, clock=lambda: 1000.0)
        self.assertAlmostEqual(budget.mood("nori")["balance"], 1.0)
        # 欠 6 點：餘額 -5，之後一般扣款被拒（等她還）
        budget.charge("nori", 6.0, reason="網路 ×4")
        self.assertAlmostEqual(budget.mood("nori")["balance"], -5.0)
        self.assertFalse(budget.try_spend("nori", 1.0, reason="自主活動"))
        # 心跳累積（時間基準）把債還完之後才恢復
        budget.accrue("nori", now=1000.0 + 30 * 6)
        self.assertAlmostEqual(budget.mood("nori")["balance"], 1.0)
        self.assertTrue(budget.try_spend("nori", 1.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
