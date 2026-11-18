#!/usr/bin/env python3
"""心跳與情緒（AEM 適配層）：她自己維持活性（owner 2026-10-22）。

判據：
  ① 有人對她說話 ⇒ valence／arousal 上升；時間過去 ⇒ 衰減（閉式投影）。
  ② 她自己的輸出不得入判定（echo 防護）。
  ③ 心跳：心情低／不寂寞 ⇒ **不說話**；心情好或寂寞 ⇒ 過閘之後才說一句。
  ④ 心跳說話要留痕（`activity` 日誌 public），別人回話時檢索得到。
  ⑤ `/api/state`（主人）看得到 `affect` 與 `heartbeat`。
"""

from __future__ import annotations

import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.state import AppState
from agent.brain.runtime.inner.affect import AffectService
from agent.brain.runtime.heartbeat import Heartbeat
from agent.brain.runtime.proactive import ProactiveGate


def service(**kwargs) -> AffectService:
    root = tempfile.mkdtemp(prefix="ba-affect-")
    return AffectService(root, enabled=True, **kwargs)


class TestAffect(unittest.TestCase):
    def test_a_message_lifts_and_then_decays(self):
        affect = service()
        noted = affect.note_message("mira", "おはよう、今日はどう？", speaker="owner")
        self.assertTrue(noted["ok"], noted)
        affect.appraise("mira", kind="interest", delta_v=0.5, delta_a=0.6,
                        confidence=0.9, source_event_id=noted["event_id"])
        warm = affect.mood("mira")
        self.assertGreater(warm["valence"], 0.05, warm)
        # arousal 是**二階核**（契約）：事件當下是 0，約 6–7 分鐘後才到峰值，之後衰減
        peak = affect.mood("mira", at=time.time() + 400)
        later = affect.mood("mira", at=time.time() + 3 * 3600)
        self.assertGreater(peak["arousal"], 0.05, peak)
        self.assertLess(later["arousal"], peak["arousal"], "峰值過後要衰減")

    def test_her_own_output_is_refused(self):
        affect = service()
        for speaker in ("self", "assistant", "tts", "bot"):
            result = affect.note_message("mira", "我在說話", speaker=speaker)
            self.assertFalse(result["ok"], speaker)
            self.assertEqual(result["error"], "ECHO_SELF_SPEAKER")

    def test_idle_time_turns_into_social_need(self):
        affect = service()
        affect.touch("mira", at=1000.0)
        # 政策預設「90 分鐘後開始算寂寞」，兩小時後應該有明顯 social_need
        mood = affect.mood("mira", at=1000.0 + 7200)
        self.assertGreater(mood["social_need"], 0.0, mood)
        self.assertGreater(mood["propensity"], 0.0, mood)

    def test_state_is_per_entity(self):
        affect = service()
        noted = affect.note_message("aster", "調べてくれてありがとう", speaker="owner")
        affect.appraise("aster", kind="pleasure", delta_v=0.6, delta_a=0.5,
                        confidence=0.9, source_event_id=noted["event_id"])
        self.assertGreater(affect.mood("aster")["valence"], 0.05)
        self.assertEqual(affect.mood("mira")["valence"], 0.0, "另一個角色不該被帶動")


class TestHeartbeat(unittest.TestCase):
    def setUp(self):
        self.sent: list[dict] = []
        self.gate = ProactiveGate(min_gap_s=0, daily_max=99, quiet_start=0, quiet_end=0)
        self.affect = service()

    def _beat(self, **kwargs) -> Heartbeat:
        return Heartbeat(entity="mira", affect=self.affect, gate=self.gate,
                         publish=lambda who, frame: self.sent.append(frame),
                         interval_s=999, **kwargs)

    def test_quiet_heart_does_not_speak(self):
        beat = self._beat(threshold=0.55)
        self.assertEqual(beat.tick(), [], "心情平平又不久沒人理 ⇒ 不該開口")
        self.assertEqual(self.sent, [])

    def test_warm_and_lonely_heart_speaks(self):
        notified = self.affect.note_message("mira", "いいことあった？", speaker="owner")
        self.affect.appraise("mira", kind="pleasure", delta_v=0.9, delta_a=0.8,
                             confidence=0.9, source_event_id=notified["event_id"])
        self.affect.touch("mira", at=time.time() - 4 * 3600)
        beat = self._beat(threshold=0.4)
        frames = beat.tick()
        self.assertTrue(frames, "心情好又寂寞 ⇒ 應該主動說一句")
        self.assertEqual(self.sent[0]["kind"], "proactive")
        self.assertTrue(self.sent[0]["text"].strip())

    def test_heartbeat_writes_public_log(self):
        log = _FakeLog()
        notified = self.affect.note_message("mira", "ねえ", speaker="owner")
        self.affect.appraise("mira", kind="interest", delta_v=0.9, delta_a=0.9,
                             confidence=0.9, source_event_id=notified["event_id"])
        self.affect.touch("mira", at=time.time() - 4 * 3600)
        beat = self._beat(threshold=0.3, activity_log=log)
        beat.tick()
        self.assertTrue(log.rows, "心跳說的話要留痕")
        self.assertEqual(log.rows[0]["visibility"], "public")

    def test_gate_still_wins(self):
        blocked = ProactiveGate(min_gap_s=0, daily_max=0, quiet_start=0, quiet_end=0)
        notified = self.affect.note_message("mira", "ねえ", speaker="owner")
        self.affect.appraise("mira", kind="pleasure", delta_v=0.9, delta_a=0.9,
                             confidence=0.9, source_event_id=notified["event_id"])
        self.affect.touch("mira", at=time.time() - 4 * 3600)
        beat = Heartbeat(entity="mira", affect=self.affect, gate=blocked,
                          publish=lambda who, frame: self.sent.append(frame),
                          threshold=0.2)
        self.assertEqual(beat.tick(), [], "日上限 0 ⇒ 就算想說也不能說")


class _FakeLog:
    def __init__(self):
        self.rows: list[dict] = []

    def append(self, entity, *, kind, text, visibility="private", activity="", step=0,
               extra=None):
        row = {"entity": entity, "kind": kind, "text": text, "visibility": visibility}
        self.rows.append(row)
        return row


class TestStateExposesMood(unittest.TestCase):
    def test_owner_sees_affect_and_heartbeat(self):
        from agent.server import api

        demo = build(data_dir=tempfile.mkdtemp(prefix="ba-affect-state-"),
                     step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        state = AppState(demo)
        status, body = api.get_state(state, {"entity": ["mira"]},
                                     auth=demo.auth.owner_context())
        self.assertEqual(status, 200)
        self.assertIn("affect", body)
        self.assertIn("propensity", body["affect"])
        self.assertIn("heartbeat", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
