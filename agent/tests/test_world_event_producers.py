#!/usr/bin/env python3
"""世界事件的實際生產端：遊戲 API、TRPG 席位、iOS 粗位置契約。"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from agent.adapter.trpg import TrpgConfig, TrpgSeat
from agent.brain.games.session import GameStore
from agent.brain.runtime.inner.world_delta import WorldDelta
from agent.brain.runtime.inner.world_events import WorldEvents
from agent.server.api.games import post_game


class _Table:
    def __init__(self, config, views):
        self.config = config
        self.views = list(views)

    def view(self):
        return self.views.pop(0)


class TestProducers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = WorldEvents()

    def test_game_start_is_delivered_only_to_that_game_participant(self):
        state = types.SimpleNamespace(app=types.SimpleNamespace(world_events=self.events),
                                      games=GameStore())
        status, payload = post_game(state, {"op": "start", "entity": "mira"},
                                    auth=types.SimpleNamespace(principal="owner"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        root = pathlib.Path(self.tmp.name)
        owner = WorldDelta(root, "mira", events=self.events, notes_root=None)
        other = WorldDelta(root, "aster", events=self.events, notes_root=None)
        guest = WorldDelta(root, "mira", events=self.events, notes_root=None,
                           principal="guest")
        self.assertIn("五目並べ", owner.observe(now=100))
        self.assertNotIn("五目並べ", other.observe(now=100))
        self.assertNotIn("五目並べ", guest.observe(now=100))

    def test_trpg_round_change_once_and_initial_is_baseline(self):
        phases = []
        config = TrpgConfig(enabled=True, table_id="table-a", session="s1", seat="p1")
        views = [
            {"table": {"roundNo": 1}, "turn": {"seat": "p2"}},
            {"table": {"roundNo": 2}, "turn": {"seat": "p2"}},
            {"table": {"roundNo": 2}, "turn": {"seat": "p2"}},
            {"ended": True, "table": {"roundNo": 2}},
        ]
        seat = TrpgSeat(config, _Table(config, views), lambda *_args, **_kwargs: "",
                        on_phase=lambda kind, round_no: phases.append((kind, round_no)))
        for _ in range(4):
            seat.handle_kick()
        self.assertEqual(phases, [("round", 2), ("ended", 0)])

    def test_ios_location_is_consented_coarse_and_stops_on_logout(self):
        root = ROOT / "app/client/BlueAgentUnity/Assets/Scripts/Next"
        source = (root / "NextLocation.cs").read_text(encoding="utf-8")
        shell = (root / "NextShell.cs").read_text(encoding="utf-8")
        config = (root / "NextConfig.cs").read_text(encoding="utf-8")
        api = (root / "NextApi.cs").read_text(encoding="utf-8")
        plist = (ROOT / "app/client/BlueAgentUnity/Assets/Editor/IosPlistPostProcess.cs").read_text(
            encoding="utf-8")
        self.assertIn("Input.location.Start(1000f, 1000f)", source)
        self.assertIn("public static void Stop()", source)
        self.assertIn("Input.location.Stop()", source)
        self.assertIn("NextLocation.Stop()", shell, "强制 StopCoroutine 也必须停传感器")
        self.assertIn("PlayerPrefs.GetInt(KeyLocationSharing, 0)", config)
        self.assertIn('Guid.NewGuid().ToString("N")', config)
        self.assertNotIn("SystemInfo.deviceName", source)
        self.assertIn('Math.Round(lat, 2)', source)
        self.assertIn('Math.Round(lon, 2)', source)
        self.assertIn('Math.Max(1000.0, accuracy)', source)
        self.assertIn('Routine("/v1/me/location"', api)
        self.assertNotIn('Routine("/api/location"', api)
        enter = shell.split("private void Enter(JObject me)", 1)[1].split(
            "private void OnEvent", 1)[0]
        self.assertIn("RefreshLocationSharing()", enter)
        self.assertNotIn("StartLocation()", enter, "登录后必须先确认服务端授权")
        self.assertIn("NSLocationWhenInUseUsageDescription", plist)
        self.assertIn('SetBoolean("NSLocationDefaultAccuracyReduced", true)', plist)
        self.assertNotIn("NSLocationAlwaysUsageDescription", plist)


if __name__ == "__main__":
    unittest.main(verbosity=2)
