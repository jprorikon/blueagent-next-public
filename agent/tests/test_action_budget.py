#!/usr/bin/env python3
"""行動預算：三檔規則（owner 2026-10-22）。

  · 心跳 ⇒ **累積**（休息就是存錢；安靜時段照存）
  · 自主行動（自己去讀書／去玩）⇒ **消耗**
  · 他人發起（主人在端上派她進 TRPG、語音來的長鏈任務）⇒ **不受限**

判據：
  ① 餘額按**經過的時間**上升（心跳只是結算點），且不超過上限；
  ② 自主行動一步扣一次；不夠時**等她存夠**（ActivityLoop 不推進，也不報錯）；
  ③ 他人發起的回合**完全不經過預算**（呼叫端判來源，這裡根本不會被呼叫）；
  ④ 每角色一份、重啟後保留（原子寫）。
"""

from __future__ import annotations

import os

# 這幾條測的是預算／多實體推進，不測作息 ⇒ 生物鐘關掉（否則半夜會有人「睡著」）
os.environ.setdefault("BLUEAGENT_CIRCADIAN", "0")

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.state import AppState
from agent.brain.runtime.budget import ActionBudget


def budget(**kwargs) -> ActionBudget:
    return ActionBudget(Path(tempfile.mkdtemp(prefix="ba-budget-")), **kwargs)


class TestAccrual(unittest.TestCase):
    """累積是**按時間**算的（`77c820e`）：心跳間隔怎麼調，同一段時間的總量不變。"""

    def setUp(self):
        # 固定速率，免得外部環境變數讓測試隨機器漂移
        self._old_spp = os.environ.get("BLUEAGENT_BUDGET_SECONDS_PER_POINT")
        os.environ["BLUEAGENT_BUDGET_SECONDS_PER_POINT"] = "30"
        self.addCleanup(self._restore_spp)

    def _restore_spp(self):
        if self._old_spp is None:
            os.environ.pop("BLUEAGENT_BUDGET_SECONDS_PER_POINT", None)
        else:
            os.environ["BLUEAGENT_BUDGET_SECONDS_PER_POINT"] = self._old_spp

    def _budget(self, **kwargs) -> ActionBudget:
        # 假時鐘：`_load` 的 `last_at` 預設值才不會是 wall clock
        kwargs.setdefault("clock", lambda: 1000.0)
        return budget(**kwargs)

    def test_time_accrues_up_to_the_cap(self):
        pocket = self._budget(cap=10.0, start=0.0)
        for minute in (1030.0, 1060.0, 1090.0):
            pocket.accrue("mira", now=minute)
        self.assertEqual(pocket.mood("mira")["balance"], 3.0)
        pocket.accrue("mira", now=1090.0 + 30 * 20)
        self.assertEqual(pocket.mood("mira")["balance"], 10.0, "上限要守住")

    def test_accrual_does_not_depend_on_tick_count(self):
        every_30s = self._budget(cap=10.0, start=0.0)
        every_30s.accrue("mira", now=1030.0)
        every_30s.accrue("mira", now=1060.0)
        one_jump = self._budget(cap=10.0, start=0.0)
        one_jump.accrue("mira", now=1060.0)
        self.assertEqual(every_30s.mood("mira")["balance"],
                         one_jump.mood("mira")["balance"],
                         "跳幾次不該改變同一段時間的累積量")

    def test_explicit_amount_still_works(self):
        pocket = self._budget(cap=10.0, start=0.0)
        pocket.accrue("mira", 2.0)
        pocket.accrue("mira", 2.0)
        self.assertEqual(pocket.mood("mira")["balance"], 4.0)

    def test_each_entity_has_its_own_pocket(self):
        pocket = self._budget(cap=10.0, start=0.0)
        pocket.accrue("mira", now=1030.0)
        pocket.accrue("mira", now=1060.0)
        pocket.accrue("aster", now=1030.0)
        self.assertEqual(pocket.mood("mira")["balance"], 2.0)
        self.assertEqual(pocket.mood("aster")["balance"], 1.0)
        self.assertEqual(pocket.mood("nori")["balance"], 0.0)

    def test_state_survives_restart(self):
        root = Path(tempfile.mkdtemp(prefix="ba-budget-persist-"))
        first = ActionBudget(root, cap=20.0, start=0.0, clock=lambda: 1000.0)
        first.accrue("nori", now=1090.0)
        second = ActionBudget(root)
        self.assertEqual(second.mood("nori")["balance"], 3.0)
        self.assertTrue(json.loads((root / "nori.json").read_text())["entity"] == "nori")


class TestSpending(unittest.TestCase):
    def test_spend_decreases_and_records(self):
        pocket = budget(cap=10.0, start=5.0)
        self.assertTrue(pocket.try_spend("mira", 2.0, reason="去讀書"))
        self.assertEqual(pocket.mood("mira")["balance"], 3.0)
        self.assertEqual(pocket.mood("mira")["spent"], 2.0)

    def test_insufficient_returns_false_without_going_negative(self):
        pocket = budget(cap=10.0, start=1.0)
        self.assertFalse(pocket.try_spend("mira", 3.0))
        self.assertEqual(pocket.mood("mira")["balance"], 1.0)
        self.assertFalse(pocket.allows("mira", 3.0))


class TestAutonomousActivityIsGated(unittest.TestCase):
    @staticmethod
    def _settle(loop, *, quiet_s: float = 0.6, timeout: float = 20.0):
        """等她「手上那一步」真的收尾：**沒有 running 的 step**、且 cursor 連續 `quiet_s` 沒變。

        以前是固定 sleep 0.4 秒——實測每次呼叫長短差很多（0.2–2 秒，看那顆模型）：
        那一步還在跑的時候 cursor 不會動，於是它會在斷言窗口裡落地，
        看起來像「沒預算還在往前」（2026-11-03 這裡連跑三次掛兩次，基準版本一樣會掛）。
        """
        deadline = time.time() + timeout
        last = None
        since = time.time()
        while time.time() < deadline:
            activity = loop.snapshot().get("activity") or {}
            steps = activity.get("steps") or []
            busy = any(str((item or {}).get("status") or "") == "running" for item in steps)
            cursor = activity.get("cursor")
            if busy or cursor != last:
                last = cursor
                since = time.time()
            elif time.time() - since >= quiet_s:
                return cursor
            time.sleep(0.05)
        return last

    def test_activity_waits_when_the_pocket_is_empty(self):
        root = Path(tempfile.mkdtemp(prefix="ba-budget-life-"))
        demo = build(data_dir=root, step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        # 把她們的額度清空（模擬「今天已經花完了」）
        for entity in ("mira", "aster", "nori"):
            pocket = demo.budget
            while pocket.mood(entity)["balance"] > 0:
                pocket.try_spend(entity, 1.0)
        # 先讓「清空之前就已經在跑的那一步」收尾，再取基準——不然量到的是它的落地，不是預算沒生效
        for name in ("mira", "aster", "nori"):
            self._settle(demo.lives[name])
        before = {name: demo.lives[name].snapshot()["activity"]["cursor"]
                  for name in ("mira", "aster", "nori")}
        time.sleep(1.2)
        after = {name: demo.lives[name].snapshot()["activity"]["cursor"]
                 for name in ("mira", "aster", "nori")}
        self.assertEqual(before, after, "沒預算就不該自己往前推")
        # 心跳存了錢之後又能動（輪詢到真的動起來為止，不賭固定秒數）
        for entity in ("mira", "aster", "nori"):
            demo.budget.accrue(entity, 5.0)
        deadline = time.time() + 15.0
        grown = dict(before)
        while time.time() < deadline:
            grown = {name: demo.lives[name].snapshot()["activity"]["cursor"]
                     for name in ("mira", "aster", "nori")}
            if any(grown[name] > before[name] for name in grown):
                break
            time.sleep(0.1)
        self.assertTrue(any(grown[name] > before[name] for name in grown),
                        f"存夠錢要能繼續做自己的事：{before} → {grown}")


class TestExternalWorkIsFree(unittest.TestCase):
    def test_owner_work_turn_does_not_touch_the_budget(self):
        demo = build(data_dir=tempfile.mkdtemp(prefix="ba-budget-free-"),
                     step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        state = AppState(demo)
        for entity in ("mira", "aster", "nori"):
            while demo.budget.mood(entity)["balance"] > 0:
                demo.budget.try_spend(entity, 1.0)
        before = demo.budget.mood("mira")["balance"]
        # 主人派活（work 模式、來源是主人）⇒ 不該被預算擋，也不該扣
        result = state.send("資料を作っておいて", principal="owner", mode="work")
        self.assertTrue(result["frames"], "主人派的活不能因為沒預算就不做")
        self.assertEqual(demo.budget.mood("mira")["balance"], before,
                         "他人發起的工作不消耗她的自主額度")


class TestStateExposesBudget(unittest.TestCase):
    def test_api_state_has_budget(self):
        from agent.server import api

        demo = build(data_dir=tempfile.mkdtemp(prefix="ba-budget-api-"),
                     step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        state = AppState(demo)
        status, body = api.get_state(state, {"entity": ["nori"]},
                                     auth=demo.auth.owner_context())
        self.assertEqual(status, 200)
        self.assertIn("budget", body)
        self.assertEqual(body["budget"]["entity"], "nori")


class TestPriceTable(unittest.TestCase):
    """價格表（owner 2026-10-22「預算價格表」）：不同種類的自主行動價錢要不一樣。"""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix="budget-")

    def budget(self):
        from agent.brain.runtime.budget import ActionBudget
        return ActionBudget(self.root)

    def test_kinds_have_different_prices(self):
        budget = self.budget()
        self.assertEqual(budget.cost_for("ambient"), 0.5)
        self.assertEqual(budget.cost_for("study"), 1.0)
        self.assertEqual(budget.cost_for("excursion"), 2.0)

    def test_unknown_kind_falls_back_to_step_cost(self):
        budget = self.budget()
        self.assertEqual(budget.cost_for("nonsense"), budget.step_cost)

    def test_env_can_override_prices(self):
        import os
        os.environ["BLUEAGENT_BUDGET_PRICES"] = "study=1.5,play=0.8"
        self.addCleanup(os.environ.pop, "BLUEAGENT_BUDGET_PRICES", None)
        budget = self.budget()
        self.assertEqual(budget.cost_for("study"), 1.5)
        self.assertEqual(budget.cost_for("play"), 0.8)
        self.assertEqual(budget.cost_for("ambient"), 0.5, "沒列到的沿預設表")

    def test_study_costs_more_than_ambient(self):
        budget = self.budget()
        self.assertGreater(budget.cost_for("study"), budget.cost_for("ambient"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
