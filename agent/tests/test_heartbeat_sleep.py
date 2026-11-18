#!/usr/bin/env python3
"""睡眠闸（owner 2026-11-16「睡眠时间还有内心活动和心情变化，不太合理」）。

以前心跳完全没有睡眠判断：睡窗内照跳、独白和心情定位照叫模型（实测 1–6 点每人仍有
三四十次独白，间隔最短 2 分钟）。这里把 A 方案钉住：

  · `asleep` ⇒ 这一跳只做本地推进（AEM／预算／留痕照走），**一次模型都不叫**。
  · `winding`／`waking` ⇒ 独白间隔与心情定位快取年龄 ×2。
  · `force=True`（主人手动叫她）不受限；没有生物钟的场合与以前完全一致。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "agent" / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent.brain.runtime.heartbeat import Heartbeat  # noqa: E402
from agent.brain.runtime.inner import sleep_gate  # noqa: E402

NOW = 1_794_700_000.0


class FakeAffect:
    def __init__(self) -> None:
        self.advanced = 0

    def advance(self, entity):
        self.advanced += 1

    def mood(self, entity, at=None):
        return {"valence": 0.0, "arousal": 0.0, "social_nened": 0.0, "propensity": 0.0,
                "social_need": 0.0, "label": "ふつう"}


class FakeBudget:
    def __init__(self) -> None:
        self.accrued = 0

    def accrue(self, entity):
        self.accrued += 1


class FakeBeats:
    def __init__(self) -> None:
        self.marked = 0

    def mark(self, entity, **fields):
        self.marked += 1


class FakeMoodline:
    def __init__(self) -> None:
        self.calls = 0
        self.scales: list[float] = []

    def maybe_read(self, entity, mood, *, at=None, events=None, force=False, age_scale=1.0):
        self.calls += 1
        self.scales.append(float(age_scale))
        return {}


class FakeMonologue:
    def __init__(self) -> None:
        self.due_calls = 0
        self.scales: list[float] = []

    def due(self, entity, *, at=None, scale=1.0):
        self.due_calls += 1
        self.scales.append(float(scale))
        return False


class FakeGate:
    def allows(self, **kwargs):
        return False, "blocked"

    def unanswered(self, *args, **kwargs):
        return []

    def maybe_send(self, *args, **kwargs):
        return {"sent": False, "reason": "blocked"}


class SleepGateCase(unittest.TestCase):
    def build(self, state: str, *, force_state=None):
        self.affect, self.budget, self.beats = FakeAffect(), FakeBudget(), FakeBeats()
        self.moodline, mono = FakeMoodline(), FakeMonologue()
        beat = Heartbeat(entity="nori", affect=self.affect, gate=FakeGate(),
                         interval_s=60.0, threshold=0.35, budget=self.budget, beats=self.beats,
                         moodline=self.moodline, monologue=mono,
                         sleep_state=lambda entity, at: (force_state or state),
                         clock=lambda: NOW)
        beat.arousal_pacing = False
        beat.energy_brake = False
        return beat, mono

    def test_asleep_skips_every_model_call_but_keeps_local_progress(self):
        beat, mono = self.build("asleep")
        self.assertEqual(beat.tick(at=NOW), [], "睡着 ⇒ 不说、也不叫模型")
        self.assertEqual(beat.last_skip_reason, "asleep")
        self.assertEqual(self.affect.advanced, 1, "AEM 照推进")
        self.assertEqual(self.budget.accrued, 1, "预算照累积")
        self.assertEqual(self.beats.marked, 1, "心跳留痕照写")
        self.assertEqual(self.moodline.calls, 0, "不叫心情定位")
        self.assertEqual(mono.due_calls, 0, "不叫独白")

    def test_awake_behaves_as_before(self):
        beat, mono = self.build("awake")
        beat.tick(at=NOW)
        self.assertNotEqual(beat.last_skip_reason, "asleep")
        self.assertEqual(self.moodline.calls, 1, "醒着照样读心情")
        self.assertEqual(self.moodline.scales, [1.0])
        self.assertEqual(mono.due_calls, 1, "醒着照样判独白")

    def test_transition_hours_halve_the_cadence(self):
        beat, mono = self.build("winding")
        beat.tick(at=NOW)
        self.assertEqual(self.moodline.scales, [2.0], "睡前心情定位快取放宽 ×2")
        self.assertEqual(mono.scales, [2.0], "睡前独白间隔 ×2")

    def test_force_ignores_the_sleep_gate(self):
        beat, mono = self.build("asleep")
        beat.tick(at=NOW, force=True)
        self.assertNotEqual(beat.last_skip_reason, "asleep")
        self.assertEqual(mono.due_calls, 1, "主人手动叫她 ⇒ 照着走")

    def test_no_clock_means_awake(self):
        self.assertEqual(sleep_gate.from_clocks({})("nori", NOW), "awake")
        self.assertEqual(sleep_gate.cadence_scale(type("B", (), {"sleep_state": None})(), NOW), 1.0)


if __name__ == "__main__":
    unittest.main()
