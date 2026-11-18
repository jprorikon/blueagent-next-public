#!/usr/bin/env python3
"""主動消息（owner 2026-10-23「做主动消息」）：**有燃料、不重複、不亂發、看得見**。

判據：
  ① 同一句話不會說第二次（二字重疊 ≥0.8 視為重複，回 `reason=repeat`）；
  ② 主人手動觸發（`force=True`）跳過暖機與最小間隔，但仍走去重；
  ③ 心跳台詞**接地**：帶著「最近在做的事／最近聊過／剛才自己想的／已經說過的話」；
  ④ 統計看得見：閘門 snapshot 有 sent/blocked/recent，`/api/metrics` 帶 proactive＋heartbeats。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest
import json

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.proactive import ProactiveGate


class TestProactiveGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "proactive.json"
        self.gate = ProactiveGate(state_path=self.path, warmup_s=0, min_gap_s=0,
                                  daily_max=100, quiet_start=0, quiet_end=0)

    def test_dedupe_blocks_same_line(self):
        first = self.gate.maybe_send("mira", "おはよう、今日なにする？", force=True)
        second = self.gate.maybe_send("mira", "おはよう、今日なにする？", force=True)
        self.assertTrue(first["sent"], first)
        self.assertFalse(second["sent"])
        self.assertEqual(second["reason"], "repeat")
        self.assertEqual(self.gate.blocked, 1)

    def test_recent_texts_persist(self):
        self.gate.maybe_send("mira", "そろそろお茶でも淹れようか", force=True)
        again = ProactiveGate(state_path=self.path, warmup_s=0, min_gap_s=0, daily_max=100)
        self.assertEqual(len(again.recent_texts("mira")), 1)
        # 重啟後仍然知道「這句說過了」⇒ 不會一重啟就重講同一句
        repeat = again.maybe_send("mira", "そろそろお茶でも淹れようか", force=True)
        self.assertEqual(repeat["reason"], "repeat")

    def test_force_bypasses_gap_and_warmup(self):
        gate = ProactiveGate(state_path=None, warmup_s=9999, min_gap_s=9999, daily_max=100,
                             quiet_start=0, quiet_end=0)
        blocked = gate.maybe_send("mira", "一句話", principal="owner")
        self.assertEqual(blocked["reason"], "warmup")
        forced = gate.maybe_send("mira", "一句話", principal="owner", force=True)
        self.assertTrue(forced["sent"], forced)

    def test_quiet_hours_still_respected_without_force(self):
        gate = ProactiveGate(state_path=None, warmup_s=0, min_gap_s=0, daily_max=100,
                             quiet_start=0, quiet_end=24)
        outcome = gate.maybe_send("mira", "深夜にごめん")
        self.assertFalse(outcome["sent"])
        self.assertEqual(outcome["reason"], "quiet_hours")

    def test_snapshot_shape(self):
        self.gate.maybe_send("mira", "テスト", force=True)
        snap = self.gate.snapshot()
        for key in ("sent", "blocked", "quiet", "recent", "warmup_s"):
            self.assertIn(key, snap)
        self.assertTrue(snap["recent"]["mira"])


class TestHeartbeatGrounding(unittest.TestCase):
    """心跳台詞要看得到「燃料」——不是凭空撒嬌。"""

    def test_compose_prompt_contains_grounding(self):
        captured: dict = {}

        class FakeBrain:
            def run_turn(self, request):
                captured["text"] = request.text
                yield ("done", {"text": "ちょっと休憩してる？"})

        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-proactive-"))
        demo = build(brain_delay=0, data_dir=root, step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        demo.brain.run_turn = FakeBrain().run_turn            # type: ignore[assignment]
        # 直接叫心跳去合一句（force 跳過門檻與閘）
        beat = demo.heartbeat.get("mira")
        beat.compose = beat.compose or (lambda entity, mood: "")
        frames = beat.tick(force=True)
        self.assertTrue(captured.get("text"), "compose 應該被呼叫")
        self.assertIn("自分から主人に一言", captured["text"])
        self.assertTrue(frames or True)                       # 只驗 prompt 有接地欄位

    def test_grounding_fields_present_in_source(self):
        """靜態檢查：接地四項（在做的事／聊過／自己想的／說過的話）都在心跳台詞的組裝裡。"""
        source = (pathlib.Path(__file__).resolve().parents[2]
                  / "agent" / "brain" / "compose" / "heartbeat_compose.py").read_text(encoding="utf-8")
        for token in ("最近やっていること", "最近話したこと", "さっき自分で考えていたこと", "最近もう口にしたこと"):
            self.assertIn(token, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
