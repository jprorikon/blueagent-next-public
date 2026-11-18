#!/usr/bin/env python3
"""睡眠时段的衰减换速（owner 2026-11-16「睡眠中衰减率应该有所…」）。

AEM 的 v／a 是读时投影，衰减只由 `elapsed` 决定。这里钉住换速本身（纯函数）＋
`aem_v2.project(time_scale=…)` 真的按换过的秒数算：睡一半时间 ⇒ 值等于「只过了一半时间」。
没有 time_scale 的场合必须与旧行为逐位元相同。
"""

from __future__ import annotations

import pathlib
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "agent" / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent.brain.runtime.inner import sleep_decay  # noqa: E402
from agent.brain.runtime.inner.affect import DEFAULT_POLICY  # noqa: E402
from agent.brain.vendor import aem_v2  # noqa: E402

HOUR = 3600.0


def state_with_episode(*, occurred_at: float, delta_v: float, delta_a: float) -> dict:
    return {
        "revision": 1,
        "policy": {"values": dict(DEFAULT_POLICY), "revision": 1, "sha256": "x"},
        "episodes": [{"id": "ep1", "kind": "pleasure", "occurred_at": occurred_at,
                      "affect": {"delta_valence": delta_v, "delta_arousal": delta_a,
                                 "expires_at": occurred_at + 12 * HOUR},
                      "appraisal": {"confidence": 0.8}, "watch_until": None, "action": None}],
        "budget": {"reservations": []},
        "facts": {"interruptibility": None},
    }


class SpanMathCase(unittest.TestCase):
    def test_overlap_and_weighted_seconds(self):
        spans = [(100.0, 200.0)]
        self.assertEqual(sleep_decay.overlap_seconds(0.0, 1000.0, spans), 100.0)
        self.assertEqual(sleep_decay.overlap_seconds(150.0, 250.0, spans), 50.0)
        self.assertEqual(sleep_decay.overlap_seconds(200.0, 300.0, spans), 0.0)
        self.assertEqual(sleep_decay.effective_seconds(0.0, 1000.0, spans, 0.5), 950.0)
        self.assertEqual(sleep_decay.effective_seconds(0.0, 1000.0, spans, 0.0), 900.0)
        self.assertEqual(sleep_decay.effective_seconds(0.0, 1000.0, spans, 1.0), 1000.0)

    def test_provider_reads_spans_once(self):
        calls = []

        def spans():
            calls.append(1)
            return [(0.0, 10.0)]

        fn = sleep_decay.provider(spans, scale=0.0)
        fn(0.0, 10.0)
        fn(0.0, 10.0)
        self.assertEqual(len(calls), 1, "睡窗只读一次")

    def test_from_clocks_without_clock_is_identity(self):
        fn = sleep_decay.from_clocks({})("nori")
        self.assertEqual(fn(1000.0, 5000.0), 4000.0)

    def test_from_clocks_uses_clock_spans(self):
        class Clock:
            def sleep_spans(self, days=14):
                return [(2000.0, 4000.0)]

        fn = sleep_decay.from_clocks({"nori": Clock()}, scale=0.5)("nori")
        self.assertEqual(fn(1000.0, 5000.0), 4000.0 - 2000.0 * 0.5, "睡窗内那段只算一半")


class ProjectTimeScaleCase(unittest.TestCase):
    NOW = time.mktime(time.strptime("2026-11-16 07:00", "%Y-%m-%d %H:%M"))
    T0 = NOW - 8 * HOUR

    def test_without_time_scale_matches_old_behaviour(self):
        state = state_with_episode(occurred_at=self.T0, delta_v=0.5, delta_a=0.4)
        self.assertEqual(aem_v2.project(state, self.NOW), aem_v2.project(state, self.NOW, time_scale=None))

    def test_sleep_half_speed_keeps_more_of_the_feeling(self):
        state = state_with_episode(occurred_at=self.T0, delta_v=0.5, delta_a=0.4)
        normal = aem_v2.project(state, self.NOW)["affect"]
        slowed = aem_v2.project(state, self.NOW,
                                time_scale=lambda t0, t1: (t1 - t0) * 0.5)["affect"]
        self.assertGreater(slowed["valence"], normal["valence"], "睡一半时间 ⇒ 衰减一半")
        half = aem_v2.project(state, self.T0 + 4 * HOUR)["affect"]
        self.assertAlmostEqual(slowed["valence"], half["valence"], places=6,
                               msg="换速后应与「只过了一半时间」等价")
        self.assertAlmostEqual(slowed["arousal"], half["arousal"], places=6)

    def test_frozen_sleep_keeps_the_value_flat(self):
        state = state_with_episode(occurred_at=self.T0, delta_v=0.5, delta_a=0.4)
        frozen = aem_v2.project(state, self.NOW, time_scale=lambda t0, t1: 0.0)["affect"]
        fresh = aem_v2.project(state, self.T0)["affect"]
        self.assertAlmostEqual(frozen["valence"], fresh["valence"], places=6)


if __name__ == "__main__":
    unittest.main()
