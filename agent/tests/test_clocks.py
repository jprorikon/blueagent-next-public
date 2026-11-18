#!/usr/bin/env python3
"""兩個時鐘：她的時間 vs 對方的時間（owner 2026-10-22）。

判據：
  ① **不用容器時區**：容器是 UTC 也要正確（`time.localtime` 一律不用）。
  ② 不知道對方時區 ⇒ 退回**她的時區**，並標 `tz_source="entity"`（不假裝知道）。
  ③ 對方時區寫在 `data/users/<principal>.md` 的 `tz: Asia/Shanghai` ⇒ 以對方為準。
  ④ 兩邊都在深夜 ⇒ 一定不打擾。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.inner.clocks import entity_tz, hour_in, politeness, principal_tz
from agent.brain.runtime.proactive import ProactiveGate


def at_hour(hour: int, tz: str = "Asia/Tokyo") -> float:
    """造一個「在 tz 的 hour 點」的 Unix 秒。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(tz))
    moment = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return moment.timestamp()


class TestEntityClock(unittest.TestCase):
    def test_entity_tz_is_a_character_property_not_the_container(self):
        old = os.environ.get("BLUEAGENT_ENTITY_TZ")
        try:
            os.environ.pop("BLUEAGENT_ENTITY_TZ", None)
            self.assertEqual(entity_tz(), "Asia/Tokyo")
            os.environ["BLUEAGENT_ENTITY_TZ"] = "Asia/Shanghai"
            self.assertEqual(entity_tz(), "Asia/Shanghai")
        finally:
            if old is None:
                os.environ.pop("BLUEAGENT_ENTITY_TZ", None)
            else:
                os.environ["BLUEAGENT_ENTITY_TZ"] = old

    def test_hour_is_computed_in_that_zone(self):
        stamp = at_hour(9, "Asia/Tokyo")
        self.assertEqual(hour_in("Asia/Tokyo", stamp), 9)
        self.assertEqual(hour_in("UTC", stamp), 0, "同一個瞬間在 UTC 是 0 點")


class TestPoliteness(unittest.TestCase):
    def setUp(self):
        self.users = Path(tempfile.mkdtemp(prefix="ba-clock-"))

    def test_unknown_principal_falls_back_to_her_clock(self):
        window = politeness(at_hour(3), entity="Asia/Tokyo", principal="owner",
                            users_dir=self.users)
        self.assertTrue(window["quiet"])
        self.assertEqual(window["tz_source"], "entity")
        self.assertEqual(window["principal_tz"], "")

    def test_known_principal_timezone_wins(self):
        (self.users / "owner.md").write_text(
            "# owner\n\n## identity\nオーナー\ntz: America/New_York\n", encoding="utf-8")
        stamp = at_hour(9, "Asia/Tokyo")          # JST 09:00 = NY 20:00（前一日）
        window = politeness(stamp, entity="Asia/Tokyo", principal="owner",
                            users_dir=self.users)
        self.assertEqual(window["tz_source"], "principal")
        self.assertEqual(window["principal_tz"], "America/New_York")
        self.assertFalse(window["quiet"], window)

    def test_principal_tz_parser(self):
        (self.users / "guest-a.md").write_text("## identity\nゲスト\ntz: Asia/Shanghai\n",
                                               encoding="utf-8")
        self.assertEqual(principal_tz("guest-a", users_dir=self.users), "Asia/Shanghai")
        self.assertEqual(principal_tz("nobody", users_dir=self.users), "")


class TestHeartbeatKeepsBeatingInQuietHours(unittest.TestCase):
    """owner 2026-10-22：「安静时段她也维持心跳」——只是不出声。"""

    def test_ticks_and_state_advance_but_no_message(self):
        from agent.brain.runtime.inner.affect import AffectService
        from agent.brain.runtime.heartbeat import Heartbeat

        affect = AffectService(tempfile.mkdtemp(prefix="ba-quiet-"))
        noted = affect.note_message("mira", "ねえ", speaker="owner")
        affect.appraise("mira", kind="pleasure", delta_v=0.9, delta_a=0.9,
                        confidence=0.9, source_event_id=noted["event_id"])
        gate = ProactiveGate(min_gap_s=0, daily_max=99, quiet_start=23, quiet_end=7)
        sent: list[dict] = []
        beat = Heartbeat(entity="mira", affect=affect, gate=gate,
                         publish=lambda who, frame: sent.append(frame), threshold=0.0)
        night = at_hour(3, "Asia/Tokyo")
        before = affect.mood("mira", at=night)["revision"]
        beat.tick(at=night)
        beat.tick(at=night + 30)
        self.assertEqual(sent, [], "安靜時段不該出聲")
        self.assertEqual(beat.ticks, 2, "但心跳要照跳")
        self.assertGreater(beat.skipped, 0)
        after = affect.mood("mira", at=night)["revision"]
        self.assertGreaterEqual(after, before, "情緒狀態照樣在推進（衰減／重算）")


class TestGateUsesClocks(unittest.TestCase):
    def test_quiet_hours_uses_entity_clock_when_principal_unknown(self):
        gate = ProactiveGate(min_gap_s=0, daily_max=9, quiet_start=23, quiet_end=7)
        night = at_hour(3, "Asia/Tokyo")
        ok, reason = gate.allows("mira", now=night, principal="owner")
        self.assertFalse(ok)
        self.assertEqual(reason, "quiet_hours")
        self.assertEqual(gate.last_window["tz_source"], "entity")

    def test_daytime_is_allowed(self):
        gate = ProactiveGate(min_gap_s=0, daily_max=9, quiet_start=23, quiet_end=7)
        ok, reason = gate.allows("mira", now=at_hour(11, "Asia/Tokyo"),
                                 principal="owner")
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
