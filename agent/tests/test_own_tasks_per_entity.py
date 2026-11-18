#!/usr/bin/env python3
"""「自己的任務」歸屬到角色，**不互串**（owner 2026-10-22）。

一個角色身上有六樣東西，全部必須是 per-entity，任何一項共用就會串：

  ① 活動本體（`data/activities/<entity>.json`）
  ② 活動日誌（`data/logs/activity-<entity>.jsonl`）
  ③ 情緒狀態（`data/affect/<entity>/…`）
  ④ 心跳（每角色一條 thread）
  ⑤ 任務佇列（scheduler 以 `decision.key.entity` 分槽）
  ⑥ 記憶寫入（每條事實帶 entity／speaker）

判據：跑一輪三個人的活動之後——
  · 每個人的檔案只有自己的標題與自己的紀錄；
  · 別人檔案裡**一個字都沒被寫進去**；
  · 各自的情緒只被自己的活動推動。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.life import presets_for
from agent.brain.compose.state import AppState


class TestTasksArePerEntity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="ba-own-tasks-"))
        cls.app = build(data_dir=cls.root, step_duration=0.02, life_pause=0.005)
        # 讓三條 life 各推幾步（步長 0.02 s）：等到「三個人都真的有紀錄」再驗
        deadline = time.time() + 10
        while time.time() < deadline:
            logs = list((cls.root / "logs").glob("activity-*.jsonl"))
            if len(logs) >= 3:
                break
            time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown_lives()

    def test_each_activity_file_belongs_to_its_own_entity(self):
        for name in ("mira", "aster", "nori"):
            path = self.root / "activities" / f"{name}.json"
            self.assertTrue(path.is_file(), f"{name} 應該有自己的活動檔")
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["entity"], name, f"{path.name} 裡的 entity 必須是自己")

    def test_activity_titles_follow_each_own_preset(self):
        """標題必須出自**自己的活動庫**（輪換後可能換套，所以比庫不比單一預設）。"""
        for name in ("mira", "aster", "nori"):
            raw = json.loads((self.root / "activities" / f"{name}.json").read_text(encoding="utf-8"))
            allowed = {item.title for item in presets_for(name)}
            self.assertIn(raw["title"], allowed, f"{name}: {raw['title']}")
            others = {item.title for other in ("mira", "aster", "nori") if other != name
                      for item in presets_for(other)}
            self.assertNotIn(raw["title"], others, f"{name} 拿到別人的活動了")

    def test_activity_logs_do_not_leak_between_entities(self):
        logs = {path.name: path for path in (self.root / "logs").glob("activity-*.jsonl")}
        self.assertTrue(logs, "應該有活動日誌")
        for filename, path in logs.items():
            owner = filename.replace("activity-", "").replace(".jsonl", "")
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                self.assertEqual(row["entity"], owner,
                                 f"{filename} 裡混進了 {row['entity']} 的紀錄")

    def test_affect_is_not_shared(self):
        from agent.brain.runtime.inner.affect import AffectService

        affect = self.app.affect
        self.assertIsInstance(affect, AffectService)
        # 有對話才會有狀態檔（沒聊過的角色不該先被建一份）
        state = AppState(self.app)
        for name in ("mira", "aster", "nori"):
            state.send("ねえ", entity=name, principal="owner")
        moods = {name: affect.mood(name) for name in ("mira", "aster", "nori")}
        for name, mood in moods.items():
            self.assertEqual(mood["entity"], name)
        # 目錄也分開（同一份狀態被三個角色共用就是串）
        dirs = {path.name for path in (self.root / "affect").glob("*") if path.is_dir()}
        self.assertTrue({"mira", "aster", "nori"} <= dirs, dirs)

    def test_heartbeat_is_one_per_entity(self):
        beats = self.app.heartbeat.beats
        self.assertEqual(set(beats), {"mira", "aster", "nori"})
        for name, beat in beats.items():
            self.assertEqual(beat.entity, name)
            self.assertEqual(beat._worker.name if beat._worker else "", "")  # 未 start 時沒有 thread


class TestSchedulerSeparatesEntities(unittest.TestCase):
    def test_jobs_are_laned_per_entity(self):
        demo = build(data_dir=tempfile.mkdtemp(prefix="ba-lane-"),
                     step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        state = AppState(demo)
        seen: list[str] = []
        original = demo.scheduler.submit

        def spy(decision_, fn, **kwargs):
            seen.append(str(getattr(getattr(decision_, "key", None), "entity", "")))
            return original(decision_, fn, **kwargs)

        demo.scheduler.submit = spy
        state.send("mira 你好", entity="mira", principal="owner")
        state.send("aster 你好", entity="aster", principal="owner")
        self.assertEqual(seen[:2], ["mira", "aster"], "兩個角色的工作要進各自的槽")


def demo_activity_cursor(demo, entity: str) -> int:
    loop = (getattr(demo, "lives", {}) or {}).get(entity)
    if loop is None:
        return 0
    snapshot = loop.snapshot()
    activity = snapshot.get("activity") or {}
    return int(activity.get("cursor") or 0) + (1 if activity else 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
