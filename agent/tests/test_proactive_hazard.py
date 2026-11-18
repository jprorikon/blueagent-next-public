#!/usr/bin/env python3
"""機率化搭話（owner 2026-11-11「目标日均条数20条」「按时间提升概率」）。

判據：
  ① 「到點」之後**不是必發**：每跳按 p 擲骰，`p = 1 − exp(−λ·dt)`，等越久越可能；
  ② 時段曲線在起作用：傍晚的 p > 上午的 p（同一份心情）；
  ③ 心情乘區在起作用：social_need 高 ⇒ p 高；
  ④ 硬邊不動：安靜時段／睡著／每日上限照樣擋，而且擋在擲骰之前；
  ⑤ 關掉機率化（`hazard=False`）⇒ 回舊行為（閘放行就必發）；
  ⑥ 心跳那一側：擲輸只是「這一跳不說」，不叫模型、不算用掉額度；
  ⑦ 24 小時模擬：日均接近 20，而且不再堆在上午（舊行為是 07–11 就把 12 條用完）。
"""

from __future__ import annotations

import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.heartbeat import Heartbeat
from agent.brain.runtime.inner.affect import AffectService
from agent.brain.runtime.proactive import (PROACTIVE_CURVE, PROACTIVE_DAILY_MAX,
                                           ProactiveGate)

#: 2026-11-11（水）——用固定日期，時段判定才穩定
DAY = time.mktime((2026, 11, 11, 0, 0, 0, 0, 0, -1))
MOOD = {"social_need": 0.4, "propensity": 0.45}


def at(hour: int, minute: int = 0) -> float:
    return DAY + hour * 3600 + minute * 60


def gate(**kwargs) -> ProactiveGate:
    kwargs.setdefault("min_gap_s", 0)
    kwargs.setdefault("daily_max", PROACTIVE_DAILY_MAX)
    kwargs.setdefault("quiet_start", 0)
    kwargs.setdefault("quiet_end", 0)
    kwargs.setdefault("hazard", True)
    kwargs.setdefault("daily_target", 20)
    kwargs.setdefault("clock", lambda: at(12))
    return ProactiveGate(**kwargs)


class TestHazardShape(unittest.TestCase):
    def test_rate_follows_the_curve(self):
        """傍晚最容易開口、上午最難；基準率由「實際日均」反推（含不應期）。"""
        g = gate(min_gap_s=20 * 60)
        # 有 min_gap 的不應期 ⇒ 基準率要比「目標 ÷ Σcurve」高（不然實際只會來 14 條，實測）
        self.assertGreater(g.rate_per_hour, 20 / sum(PROACTIVE_CURVE))
        evening = g.hourly_rate("mira", now=at(18), mood=MOOD)
        morning = g.hourly_rate("mira", now=at(10), mood=MOOD)
        self.assertGreater(evening, morning)
        self.assertGreater(evening / max(morning, 1e-9), 2.0)

    def test_no_refractory_means_the_plain_division(self):
        """沒有不應期時，反推就退化成 `目標 ÷ Σcurve`（公式沒有多餘假設）。"""
        g = gate(min_gap_s=0)
        self.assertAlmostEqual(g.rate_per_hour, 20 / sum(PROACTIVE_CURVE), places=6)

    def test_probability_rises_with_waiting(self):
        """同一時段：一跳 15 秒的機率 < 一跳 75 秒（等得久 ⇒ 這一次更可能）。"""
        g = gate()
        quick = g.hazard_p("mira", now=at(18), mood=MOOD, dt=15)
        slow = g.hazard_p("mira", now=at(18), mood=MOOD, dt=75)
        self.assertLess(quick, slow)
        self.assertLess(slow, 0.2, "單跳機率不該大到「一放行就幾乎必發」")

    def test_mood_multiplies_the_rate(self):
        g = gate()
        low = g.hazard_p("mira", now=at(18), mood={"social_need": 0.05, "propensity": 0.1}, dt=45)
        high = g.hazard_p("mira", now=at(18), mood={"social_need": 0.95, "propensity": 0.9}, dt=45)
        self.assertGreater(high, low * 2)

    def test_disabled_means_always_send(self):
        """關掉機率化 ⇒ p=1、擲骰恆真（回舊行為，A/B 用）。"""
        g = gate(hazard=False, rng=lambda: 0.999999)
        self.assertEqual(g.hazard_p("mira", now=at(18), mood=MOOD, dt=45), 1.0)
        self.assertTrue(g.roll("mira", now=at(18), mood=MOOD, dt=45)[0])


class TestHardGatesStillWin(unittest.TestCase):
    def test_quiet_hours_block_before_the_roll(self):
        g = gate(quiet_start=23, quiet_end=7)
        # 03:00 她自己的安靜時段：連 `allows` 都不過，擲骰根本不會發生
        allowed, reason = g.allows("mira", now=at(3))
        self.assertFalse(allowed)
        self.assertEqual(reason, "quiet_hours")
        self.assertEqual(g.last_hazard, {})

    def test_daily_max_still_caps(self):
        g = gate(daily_max=2, rng=lambda: 0.0)
        for _ in range(2):
            g.mark("mira", now=at(18))
        allowed, reason = g.allows("mira", now=at(20))
        self.assertFalse(allowed)
        self.assertEqual(reason, "daily_max")

    def test_sleeping_entity_does_not_speak(self):
        class Clock:
            def asleep(self, now=None):
                return True
        g = gate(circadian={"mira": Clock()})
        allowed, reason = g.allows("mira", now=at(18))
        self.assertFalse(allowed)
        self.assertEqual(reason, "circadian_sleep")


class TestRollAccounting(unittest.TestCase):
    def test_roll_records_the_dice(self):
        g = gate(rng=lambda: 0.99)
        ok, info = g.roll("mira", now=at(10), mood=MOOD, dt=45)
        self.assertFalse(ok)                       # p≈1%，0.99 必輸
        self.assertIn("p", info)
        self.assertIn("rate", info)
        self.assertEqual(info["hour"], 10)
        self.assertEqual(g.last_hazard["mira"]["roll"], 0.99)
        self.assertEqual(g.hazard_skips, 1)

    def test_snapshot_exposes_hazard(self):
        g = gate(rng=lambda: 0.5)
        g.roll("mira", now=at(18), mood=MOOD, dt=45)
        snap = g.snapshot()
        self.assertTrue(snap["hazard"])
        self.assertGreater(snap["hazard_rate_per_hour"], 0)
        self.assertIn("mira", snap["hazard_last"])


class TestHeartbeatIntegration(unittest.TestCase):
    """心跳那一側：擲輸＝這一跳不說，不叫模型、不消耗額度。"""

    def build(self, rng):
        affect = AffectService(tempfile.mkdtemp(prefix="ba-hazard-"), enabled=True)
        noted = affect.note_message("mira", "ねえ", speaker="owner")
        affect.appraise("mira", kind="pleasure", delta_v=0.9, delta_a=0.5,
                        confidence=0.9, source_event_id=noted["event_id"])
        self.sent: list[dict] = []
        composed: list[int] = []

        def compose(entity, mood, due=()):
            composed.append(1)
            return "ねえ、ちょっといい？"

        beat = Heartbeat(entity="mira", affect=affect,
                         gate=gate(rng=rng),
                         publish=lambda who, frame: self.sent.append(frame),
                         compose=compose, threshold=0.0,
                         clock=lambda: at(18))
        beat.arousal_pacing = False
        return beat, composed

    def test_lost_roll_skips_without_calling_the_model(self):
        beat, composed = self.build(rng=lambda: 0.999999)
        self.assertEqual(beat.tick(), [])
        self.assertEqual(beat.last_skip_reason, "hazard")
        self.assertEqual(composed, [], "擲輸時不該叫模型組句")
        self.assertEqual(beat.last_hazard.get("ok"), False)

    def test_won_roll_sends(self):
        beat, composed = self.build(rng=lambda: 0.0)
        frames = beat.tick()
        self.assertTrue(frames, "擲贏就照舊送出去")
        self.assertEqual(len(composed), 1)
        self.assertEqual(beat.last_skip_reason, "")


class TestDailySimulation(unittest.TestCase):
    """24 小時模擬：日均 ≈ 20，且不再堆在上午（舊行為 07–11 就把 12 條用完）。"""

    def simulate(self, *, hazard: bool, seed: int = 7) -> dict:
        import random
        rng = random.Random(seed)
        g = gate(min_gap_s=20 * 60, daily_max=PROACTIVE_DAILY_MAX, quiet_start=23, quiet_end=7,
                 hazard=True, daily_target=20, rng=rng.random)
        sent = 0
        buckets = {}
        last = 0.0
        # 07:00 起、每 45 秒一跳（心跳實際間隔 15–75 秒，取中位）
        for tick in range(int(16 * 3600 / 45)):
            now = at(7) + tick * 45
            allowed, _ = g.allows("mira", now=now)
            if not allowed:
                continue
            if hazard:
                ok, _info = g.roll("mira", now=now, mood=MOOD, dt=45)
                if not ok:
                    continue
            g.mark("mira", now=now)
            sent += 1
            hour = time.localtime(now).tm_hour
            buckets[hour] = buckets.get(hour, 0) + 1
            last = now
        return {"sent": sent, "buckets": buckets, "last": last}

    def test_daily_total_is_about_twenty(self):
        counts = [self.simulate(hazard=True, seed=seed)["sent"] for seed in (1, 2, 3, 4, 5)]
        average = sum(counts) / len(counts)
        self.assertGreaterEqual(average, 8, counts)
        self.assertLessEqual(average, 40, counts)

    def test_morning_no_longer_eats_the_whole_day(self):
        """舊行為（hazard=False）：07–11 幾乎發滿每日上限；機率化後上午只佔少數、傍晚才是峰。"""
        old = self.simulate(hazard=False, seed=1)
        old_morning = sum(count for hour, count in old["buckets"].items() if 7 <= hour <= 11)
        self.assertGreaterEqual(old_morning, 10, "舊行為本來就會在上午吃掉大半額度")

        new = self.simulate(hazard=True, seed=1)
        morning = sum(count for hour, count in new["buckets"].items() if 7 <= hour <= 11)
        evening = sum(count for hour, count in new["buckets"].items() if 17 <= hour <= 22)
        self.assertLess(morning / max(1, new["sent"]), 0.45,
                        f"上午佔比仍太高：{new['buckets']}")
        self.assertGreater(evening, morning, f"傍晚應該比上午多：{new['buckets']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
