#!/usr/bin/env python3
"""天氣工具（沿用現役 BlueAgent）：適配器／位置倉／工具三層都要能離線驗。

判據：
  ① `describe()` 把 WMO code 與今日／明日翻成一句日語；未知 code 是「不明」，不是猜；
  ② 位置**按端末**存、`latest()` 取全機最近一次；
  ③ 工具：有座標就用座標；沒有座標走 IP 概算且**明說概算**；兩者都沒有 ⇒ `no_location`（不編天氣）；
  ④ 給了 `place` 就只查那個地名（不反問、不猜）。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter import weather
from agent.brain.runtime.locations import LocationStore
from agent.brain.runtime.tools import ToolContext, ToolRegistry
from agent.adapter.weather_tool import GetWeatherTool

FORECAST = {
    "current": {"weather_code": 61, "temperature_2m": 18.4, "apparent_temperature": 17.1,
                "relative_humidity_2m": 72, "wind_speed_10m": 3.2},
    "daily": {"weather_code": [61, 3], "temperature_2m_max": [21.0, 23.5],
              "temperature_2m_min": [14.2, 15.8]},
}


def ctx() -> ToolContext:
    return ToolContext(entity="mira", principal="owner", thread_id="th-1",
                       source="app", voice=None)


class TestWeatherAdapter(unittest.TestCase):
    def setUp(self):
        self._orig = weather._get_json
        weather._get_json = lambda url, params=None, timeout=weather.TIMEOUT: dict(FORECAST)
        self.addCleanup(lambda: setattr(weather, "_get_json", self._orig))

    def test_describe_is_one_line_japanese(self):
        text = weather.describe(35.68, 139.76, label="東京")
        self.assertIn("東京", text)
        self.assertIn("雨", text)
        self.assertIn("18.4", text)
        self.assertIn("今日", text)
        self.assertIn("明日", text)

    def test_unknown_code_is_not_guessed(self):
        self.assertEqual(weather.code_ja(12345), "不明（code 12345）")
        self.assertEqual(weather.code_ja("x"), "不明")

    def test_no_label_means_no_place_name(self):
        text = weather.describe(35.68, 139.76)
        self.assertNotIn("東京", text)


class TestLocationStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocationStore(pathlib.Path(self.tmp.name))

    def test_save_and_latest_by_device(self):
        self.store.save("phone", 35.0, 139.0, principal="owner", at=100.0)
        self.store.save("tablet", 34.0, 135.0, principal="owner", at=200.0)
        self.assertEqual(self.store.get("phone")["lat"], 35.0)
        latest = self.store.latest()
        self.assertEqual(latest["device"], "tablet")   # 取最近一次（跨端末）


class TestWeatherTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LocationStore(pathlib.Path(self.tmp.name))
        self._orig = weather._get_json
        weather._get_json = lambda url, params=None, timeout=weather.TIMEOUT: dict(FORECAST)
        self.addCleanup(lambda: setattr(weather, "_get_json", self._orig))

    def test_uses_reported_location(self):
        self.store.save("phone", 35.68, 139.76, principal="owner")
        tool = GetWeatherTool(self.store)
        result = tool.run(ctx(), {})
        self.assertTrue(result.ok, result.error)
        self.assertIn("雨", result.data["text"])
        self.assertFalse(result.data["coarse"])

    def test_falls_back_to_ip_and_says_so(self):
        tool = GetWeatherTool(self.store)
        original = weather.ip_locate
        weather.ip_locate = lambda ip="": {"lat": 35.0, "lon": 139.0, "label": "東京",
                                           "coarse": True}
        self.addCleanup(lambda: setattr(weather, "ip_locate", original))
        result = tool.run(ctx(), {})
        self.assertTrue(result.ok)
        self.assertTrue(result.data["coarse"])
        self.assertIn("概算", result.data["text"])

    def test_no_location_refuses_to_invent(self):
        tool = GetWeatherTool(self.store)
        original = weather.ip_locate
        weather.ip_locate = lambda ip="": None
        self.addCleanup(lambda: setattr(weather, "ip_locate", original))
        result = tool.run(ctx(), {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_location")

    def test_place_argument_is_respected(self):
        tool = GetWeatherTool(self.store)
        original = weather.geocode
        weather.geocode = lambda place: {"label": "大阪", "lat": 34.7, "lon": 135.5}
        self.addCleanup(lambda: setattr(weather, "geocode", original))
        result = tool.run(ctx(), {"place": "大阪"})
        self.assertTrue(result.ok)
        self.assertIn("大阪", result.data["text"])

    def test_tool_is_open_to_guests_in_chat(self):
        registry = ToolRegistry().register(GetWeatherTool(self.store))
        tool = registry.get("weather.get")
        self.assertTrue(registry.allowed(tool, mode="chat", is_owner=False))
        self.assertTrue(registry.allowed(tool, mode="work", is_owner=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
