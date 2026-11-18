#!/usr/bin/env python3
"""C-14 Agent 位置组合仓：角色凭证隔离、MR 保留、旧手机样本不复活。"""

from __future__ import annotations

import pathlib
import tempfile
import types
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter import weather
from agent.adapter.app_locations import PlatformLocationStore, build_location_store
from agent.adapter.weather_tool import GetWeatherTool
from agent.brain.domain.auth import AuthContext
from agent.brain.domain.client_end import MR
from agent.brain.runtime.locations import LocationStore
from agent.brain.runtime.tools import ToolContext
from agent.server.api.turns import post_location


def remote(account="owner", *, at=900.0, expires=1800.0, accuracy=1000.0,
           precision="coarse", lat=35.68, lon=139.77):
    return {"account_id": account, "principal": account, "device": "random-device-id",
            "lat": lat, "lon": lon, "accuracy": accuracy, "at": at,
            "expires_at": expires, "source": "ios", "precision": precision,
            "purpose": "weather-v1"}


class FakeClient:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.calls = []

    def board_call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.error is not None:
            raise self.error
        return {"location": self.row}


class PlatformLocationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-platform-location-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.legacy = LocationStore(self.root / "locations")

    def store(self, clients):
        return PlatformLocationStore(self.legacy, clients, clock=lambda: 1000.0)

    def test_each_entity_uses_only_its_own_credential_and_principal(self):
        aster, nori = FakeClient(remote()), FakeClient(remote(at=950.0, lat=34.68))
        store = self.store({"aster": aster, "nori": nori})
        self.assertEqual(store.latest(principal="owner", entity="aster")["lat"], 35.68)
        self.assertEqual(store.latest(principal="owner", entity="nori")["lat"], 34.68)
        self.assertEqual(aster.calls, [("location.get", {"principal": "owner"})])
        self.assertEqual(nori.calls, [("location.get", {"principal": "owner"})])
        self.assertIsNone(store.latest(principal="owner", entity="unknown"))

    def test_expired_precise_and_nonfinite_remote_rows_are_rejected(self):
        rows = [remote(expires=1000.0), remote(accuracy=999.9),
                remote(precision="fine"), remote(lat=float("nan")),
                remote(lon=float("inf")), remote(accuracy=float("nan"))]
        for row in rows:
            with self.subTest(row=row):
                self.assertIsNone(self.store({"aster": FakeClient(row)}).latest(
                    principal="owner", entity="aster"))

    def test_old_phone_sample_never_revives_but_mr_survives_remote_failure(self):
        self.legacy.save("old-phone", 1.0, 2.0, principal="owner", at=990.0,
                         source="gps")
        offline = self.store({"aster": FakeClient(error=RuntimeError("offline"))})
        self.assertIsNone(offline.latest(principal="owner", entity="aster"))
        # SharedWorld 的无 entity 旧路径保持原行为，由后续缓存策略继续处理。
        self.assertEqual(offline.latest(principal="owner")["device"], "old-phone")

        self.legacy.save("mr-headset", 35.0, 139.0, principal="owner", at=800.0,
                         source="mr:gps")
        fallback = offline.latest(principal="owner", entity="aster")
        self.assertEqual((fallback["device"], fallback["source"]), ("mr-headset", "mr:gps"))
        current = self.store({"aster": FakeClient(remote(at=900.0))}).latest(
            principal="owner", entity="aster")
        self.assertEqual(current["device"], "random-device-id")
        older_remote = self.store({"aster": FakeClient(remote(at=700.0))}).latest(
            principal="owner", entity="aster")
        self.assertEqual(older_remote["device"], "mr-headset")

    def test_legacy_endpoint_marks_only_mr_source(self):
        state = types.SimpleNamespace(app=types.SimpleNamespace(locations=self.legacy))
        auth = AuthContext(principal="owner")
        body = {"device": "headset", "lat": 35.0, "lon": 139.0,
                "accuracy": 1000, "source": "gps"}
        status, _ = post_location(state, body, auth=auth, source=MR)
        self.assertEqual(status, 200)
        self.assertEqual(self.legacy.get("headset")["source"], "mr:gps")
        status, _ = post_location(state, dict(body, device="old-phone"), auth=auth,
                                  source="phone")
        self.assertEqual(status, 200)
        self.assertEqual(self.legacy.get("old-phone")["source"], "gps")

    def test_builder_keeps_legacy_without_app_configuration(self):
        self.assertIs(build_location_store(self.legacy, {}), self.legacy)
        wrapped = build_location_store(self.legacy, {
            "BLUEAGENT_APP_SERVER_URL": "http://127.0.0.1:8788",
            "BLUEAGENT_APP_SERVER_TOKENS": "aster=one,nori=two",
        })
        self.assertIsInstance(wrapped, PlatformLocationStore)
        self.assertEqual(set(wrapped.clients), {"aster", "nori"})

    def test_weather_tool_passes_principal_and_entity_to_store(self):
        calls = []

        class SpyLocations:
            def latest(self, **kwargs):
                calls.append(kwargs)
                return None

        original = weather.ip_locate
        weather.ip_locate = lambda _ip="": None
        self.addCleanup(lambda: setattr(weather, "ip_locate", original))
        context = ToolContext(entity="aster", principal="guest", thread_id="th", source="app",
                              voice=None)
        result = GetWeatherTool(SpyLocations()).run(context, {})
        self.assertFalse(result.ok)
        self.assertEqual(calls, [{"principal": "guest", "entity": "aster"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
