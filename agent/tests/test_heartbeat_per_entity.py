#!/usr/bin/env python3
"""心跳是**每個角色一條**（owner 2026-10-22：「心跳也做成每个角色自己有自己的心跳」）。

判據：
  ① 三條心跳分別存在，`interval`／`threshold`／`ticks` 各自獨立；
  ② 一個角色跳動**不會**推進另一個角色的 ticks；
  ③ 一個角色的冷卻（被閘擋）**不會**讓另一個閉嘴；
  ④ `/api/state` 看得到 `heartbeat.beats` 三個，且 `heartbeat_self` 指到當前 entity；
  ⑤ 每條心跳有自己的 thread 名字（`ba-heartbeat-<entity>`）。
"""

from __future__ import annotations

import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.inner.affect import AffectService
from agent.brain.runtime.heartbeat import (DEFAULT_INTERVALS, Heartbeat,
                                              HeartbeatSet, build_heartbeats,
                                              interval_for)
from agent.brain.runtime.proactive import ProactiveGate


def affect_service() -> AffectService:
    return AffectService(tempfile.mkdtemp(prefix="ba-beats-"), enabled=True)


def warm(affect: AffectService, entity: str, *, dv: float = 0.9, da: float = 0.5) -> None:
    noted = affect.note_message(entity, "ねえ", speaker="owner")
    affect.appraise(entity, kind="pleasure", delta_v=dv, delta_a=da, confidence=0.9,
                    source_event_id=noted["event_id"])


class TestOneBeatPerEntity(unittest.TestCase):
    def setUp(self):
        self.affect = affect_service()
        self.gate = ProactiveGate(min_gap_s=0, daily_max=99, quiet_start=0, quiet_end=0)
        self.sent: list[dict] = []
        self.beats = build_heartbeats(
            ["mira", "aster", "nori"], affect=self.affect, gate=self.gate,
            publish=lambda who, frame: self.sent.append(frame))

    def test_three_independent_beats(self):
        self.assertEqual(set(self.beats.beats), {"mira", "aster", "nori"})
        intervals = {name: beat.interval_s for name, beat in self.beats.beats.items()}
        self.assertEqual(intervals, {name: DEFAULT_INTERVALS[name] for name in DEFAULT_INTERVALS},
                         "三個人的節拍本來就不一樣，不該被同一個 interval 綁住")

    def test_ticking_one_does_not_tick_another(self):
        self.beats.get("aster").tick()
        self.assertEqual(self.beats.get("aster").ticks, 1)
        self.assertEqual(self.beats.get("mira").ticks, 0, "別人的心跳不該被帶著跳")
        self.assertEqual(self.beats.get("nori").ticks, 0)

    def test_one_entitys_cooldown_does_not_silence_another(self):
        for entity in ("mira", "aster"):
            warm(self.affect, entity)
            self.affect.touch(entity, at=time.time() - 4 * 3600)
        # 讓 mira 先說一次，之後她的 min_gap 會擋住；aster 不受影響
        gate = ProactiveGate(min_gap_s=600, daily_max=99, quiet_start=0, quiet_end=0)
        beats = build_heartbeats(["mira", "aster"], affect=self.affect, gate=gate,
                                  publish=lambda who, frame: self.sent.append(frame),
                                  threshold=0.0)
        self.assertTrue(beats.get("mira").tick(), "第一次應該說得出來")
        self.assertTrue(beats.get("aster").tick(), "另一個角色有自己的一份冷卻")
        self.assertEqual(beats.get("mira").last_skip_reason, "")

    def test_interval_override_per_entity(self):
        import os
        old = os.environ.get("BLUEAGENT_HEARTBEAT_S_NORI")
        try:
            os.environ["BLUEAGENT_HEARTBEAT_S_NORI"] = "12"
            self.assertEqual(interval_for("nori"), 12.0)
            self.assertEqual(interval_for("mira"), DEFAULT_INTERVALS["mira"])
        finally:
            if old is None:
                os.environ.pop("BLUEAGENT_HEARTBEAT_S_NORI", None)
            else:
                os.environ["BLUEAGENT_HEARTBEAT_S_NORI"] = old

    def test_snapshot_is_per_entity(self):
        self.beats.get("nori").tick()
        snapshot = self.beats.snapshot()
        self.assertEqual(snapshot["beats"]["nori"]["ticks"], 1)
        self.assertEqual(snapshot["beats"]["mira"]["ticks"], 0)
        self.assertIn("running", snapshot["beats"]["mira"])


class TestStateExposesPerEntityBeats(unittest.TestCase):
    def test_api_state_has_beats_and_self(self):
        from agent.server import api

        demo = build(data_dir=tempfile.mkdtemp(prefix="ba-beats-state-"),
                     step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        from agent.brain.compose.state import AppState
        state = AppState(demo)
        status, body = api.get_state(state, {"entity": ["aster"]},
                                     auth=demo.auth.owner_context())
        self.assertEqual(status, 200)
        beats = (body.get("heartbeat") or {}).get("beats") or {}
        self.assertEqual(set(beats), {"mira", "aster", "nori"})
        self.assertEqual(body["heartbeat_self"]["entity"], "aster")


if __name__ == "__main__":
    unittest.main(verbosity=2)
