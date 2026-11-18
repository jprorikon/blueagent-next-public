#!/usr/bin/env python3
"""多角色同時在線：各走各的一套（owner 2026-10-21 的驗收場景）。

「我在叫 mira 的时候，guest 可以叫 nori」＋「mira 做 A 的时候 aster 可以做 B」。
判據是**併發不互相踩**：

  ① 每個角色一條 LifeLoop、一份自己的活動（標題/步驟/kind 都不同）；
  ② 兩個角色同時被說話 ⇒ 兩邊都有回、各自的 thread、各自的記憶池；
  ③ 動一個角色的活動（start/pause）不影響別人；
  ④ guest 可以指定角色說話（不是只能對著預設角色）；
  ⑤ 調度槽位按角色劃分：mira 那條佔滿 interactive 槽時，aster 的回合照樣能跑。
"""

from __future__ import annotations

import os

# 這幾條測的是預算／多實體推進，不測作息 ⇒ 生物鐘關掉（否則半夜會有人「睡著」）
os.environ.setdefault("BLUEAGENT_CIRCADIAN", "0")

import threading
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.bootstrap import build
from agent.brain.runtime.life import build_activity, presets_for
from agent.brain.domain.auth import AuthContext, Role
from agent.brain.domain.decision import DispatchMode, Engagement, RouteDecision
from agent.brain.domain.event import SourceClass
from agent.brain.domain.ids import ThreadKey
from agent.brain.runtime.scheduler import Scheduler, SchedulerConfig
from agent.server import api as api_module

GUEST = AuthContext(principal="guest-x", role=Role.GUEST, capabilities=frozenset({"chat"}))
ENTITIES = ("mira", "aster", "nori")


def decision(entity: str, thread: str, lane: SourceClass = SourceClass.INTERACTIVE,
             priority: int = 10) -> RouteDecision:
    return RouteDecision(
        thread_id=thread, key=ThreadKey("owner", entity, "together", "chat"),
        pipeline="chat.text", lane=lane, priority=priority,
        mode=DispatchMode.ATTACH, scope=None, deadline_ms=8000,
        reason=f"test:{entity}", created=False, at=time.time(), idle_s=0.0,
        source="phone:owner", principal="owner", engage=Engagement.REPLY,
        addressed_to="", engage_reason="test", yield_ms=0)


class TestEachEntityHasItsOwnLife(unittest.TestCase):
    def setUp(self):
        # 用乾淨的 data/：不然會接上 repo 裡存檔的舊活動（那是「續跑」而不是「新播」）
        self.root = tempfile.mkdtemp(prefix="ba-lives-")
        self.app = build(brain_delay=0, data_dir=self.root, step_duration=0.02,
                          life_pause=0.005)
        self.addCleanup(self.app.shutdown_lives)

    def test_three_lives_online(self):
        self.assertEqual(set(self.app.lives), set(ENTITIES))

    def test_each_activity_is_its_own(self):
        titles = {entity: (loop.snapshot().get("activity") or {}).get("title")
                  for entity, loop in self.app.lives.items()}
        self.assertEqual(len(set(titles.values())), len(ENTITIES), titles)
        for entity in ENTITIES:
            self.assertIn(titles[entity], [item.title for item in presets_for(entity)])

    def test_activity_preset_differs_per_entity(self):
        for entity in ENTITIES:
            activity = build_activity(entity)
            library = presets_for(entity)
            self.assertEqual(activity.entity, entity)
            self.assertIn(activity.title, [item.title for item in library])
            self.assertIn(activity.kind, [item.kind for item in library])
            self.assertTrue(activity.meta.get("preset"), "要記住這是哪一套（輪換用）")

    def test_lives_advance_independently(self):
        """三條 loop 各自在推自己的游標，互不等待。"""
        def cursor(entity):
            activity = self.app.lives[entity].snapshot()["activity"] or {}
            return activity.get("cursor", 0)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(cursor(entity) >= 1 for entity in ENTITIES):
                break
            time.sleep(0.05)
        self.assertTrue(all(cursor(entity) >= 1 for entity in ENTITIES),
                        {entity: cursor(entity) for entity in ENTITIES})


class TestTwoEntitiesAtOnce(unittest.TestCase):
    def setUp(self):
        self.app = build(brain_delay=0, step_duration=0.02, life_pause=0.005)
        self.addCleanup(self.app.shutdown_lives)
        from agent.brain.compose.state import AppState
        self.state = AppState(self.app)

    def test_owner_talks_to_mira_while_guest_talks_to_nori(self):
        results: dict[str, dict] = {}

        def talk(tag, **kwargs):
            results[tag] = self.state.send(**kwargs)

        threads = [
            threading.Thread(target=talk, args=("owner",), kwargs=dict(
                text="今晚一起准备角色卡吧", entity="mira", principal="owner")),
            threading.Thread(target=talk, args=("guest",), kwargs=dict(
                text="ノリ、今日の観測はどう？", entity="nori", principal="guest-x")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(set(results), {"owner", "guest"})
        mira = results["owner"]["decision"]
        nori = results["guest"]["decision"]
        self.assertEqual(mira["entity"], "mira")
        self.assertEqual(nori["entity"], "nori")
        self.assertEqual(nori["principal"], "guest-x")
        self.assertNotEqual(mira["thread"], nori["thread"], "两个角色不能共用一条线")
        self.assertIn("blueagent-mira", mira["scope"]["shared_bank"])
        self.assertIn("blueagent-nori", nori["scope"]["shared_bank"])

    def test_guest_can_pick_any_entity_through_the_http_gate(self):
        for entity in ENTITIES:
            status, data = api_module.prepare_send(
                self.state, {"text": "元気？", "entity": entity}, auth=GUEST)
            self.assertEqual(status, 200, entity)
            self.assertEqual(data["entity"], entity)
            self.assertEqual(data["mode"], "chat")
            self.assertFalse(data["admin"])

    def test_activity_control_is_per_entity(self):
        before = self.app.lives["mira"].snapshot()["activity"]["title"]
        self.state.start_activity(entity="aster")
        self.assertEqual(self.app.lives["mira"].snapshot()["activity"]["title"], before)
        self.assertIn(self.app.lives["aster"].snapshot()["activity"]["title"],
                      [item.title for item in presets_for("aster")])

    def test_snapshot_exposes_every_life(self):
        snapshot = self.state.snapshot()
        self.assertEqual(set(snapshot["lives"]), set(ENTITIES))
        self.assertEqual(snapshot["life"]["entity"], "mira", "life 保留成预设角色那一条")


class TestSchedulerSlotsPerEntity(unittest.TestCase):
    """槽位是「角色 × 源類」的預算，不是全域配額。"""

    def test_other_entity_is_not_blocked_by_a_busy_one(self):
        scheduler = Scheduler(SchedulerConfig(interactive_slots=1), entities=3)
        self.addCleanup(scheduler.shutdown)
        gate = threading.Event()
        started: list[str] = []

        def slow(tag):
            def run(token):
                started.append(tag)
                gate.wait(3)
            return run

        first = scheduler.submit(decision("mira", "th-mira"), slow("mira"))
        second = scheduler.submit(decision("aster", "th-aster"), slow("aster"))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(started) < 2:
            time.sleep(0.02)
        self.assertEqual(sorted(started), ["aster", "mira"],
                         "mira 佔住槽位時 aster 也該能跑")
        gate.set()
        first.wait(3)
        second.wait(3)

    def test_same_entity_respects_slot_budget(self):
        scheduler = Scheduler(SchedulerConfig(interactive_slots=1), entities=3)
        self.addCleanup(scheduler.shutdown)
        gate = threading.Event()
        started: list[str] = []

        def slow(tag):
            def run(token):
                started.append(tag)
                gate.wait(3)
            return run

        first = scheduler.submit(decision("mira", "th-1"), slow("one"))
        second = scheduler.submit(decision("mira", "th-2"), slow("two"))
        time.sleep(0.2)
        self.assertEqual(started, ["one"], "同一个角色只有一个 interactive 槽，第二条要等")
        gate.set()
        first.wait(3)
        second.wait(3)
        self.assertEqual(sorted(started), ["one", "two"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
