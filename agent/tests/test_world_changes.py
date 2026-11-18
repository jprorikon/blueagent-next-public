#!/usr/bin/env python3
"""世界變化擴展：真正的差量、一次性投遞、公開與房間邊界。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))   # 倉庫根

from agent.adapter.weather import CachedWeather
from agent.brain.runtime.activity_log import ActivityLog
from agent.brain.runtime.inner.world_delta import SharedWorld, WorldDelta, coarse_location_digest
from agent.brain.runtime.inner.world_events import WorldEvents
from agent.brain.runtime.locations import LocationStore

T0 = 1793683200.0


class _Thread:
    def __init__(self, source):
        self.last_active_at = T0
        self.sources = {source}
        self.participants = {"owner"}


class _Threads:
    def __init__(self, source="phone"):
        self.row = _Thread(source)

    def threads(self):
        return (self.row,)


class TestSharedChanges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def test_first_sample_is_baseline_and_change_is_seen_once(self):
        weather = [{"text": "晴れ、20℃", "code": 1}]
        threads = _Threads()
        events = WorldEvents()
        shared = SharedWorld(self.root, threads=threads, weather=lambda: weather[0], events=events)
        delta = WorldDelta(self.root, "mira", shared=shared, events=events, notes_root=None)
        delta.observe(now=T0)
        self.assertNotIn("天気が変わった", delta.observe(now=T0 + 300))
        weather[0] = {"text": "雨、18℃", "code": 63}
        first = delta.observe(now=T0 + 600)
        self.assertIn("天気が変わった", first)
        self.assertNotIn("天気が変わった", delta.block(now=T0 + 600))
        self.assertNotIn("天気が変わった", delta.observe(now=T0 + 900))

    def test_owner_device_change_and_location_digest_stay_out_of_public(self):
        threads = _Threads()
        locations = LocationStore(self.root / "locations")
        locations.save("guest-phone", 1.0, 1.0, principal="guest")
        locations.save("owner-phone", 35.68, 139.76, principal="owner")
        events = WorldEvents()
        shared = SharedWorld(self.root, threads=threads, locations=locations, events=events)
        delta = WorldDelta(self.root, "mira", shared=shared, events=events, notes_root=None)
        delta.observe(now=T0)
        threads.row.sources = {"app"}
        locations.save("owner-phone", 35.78, 139.76, principal="owner")
        text = delta.observe(now=T0 + 600)
        self.assertIn("スマホからアプリに移った", text)
        self.assertIn("場所が変わった", delta.observe(now=T0 + 601))
        public = delta.block(now=T0 + 602, public=True)
        self.assertNotIn("主人", public)
        self.assertNotIn("場所", public)
        raw = shared.path().read_text(encoding="utf-8")
        self.assertNotIn("35.7", raw)
        self.assertNotIn("139.7", raw)
        self.assertNotIn("guest-phone", raw)
        digest = shared.read()["location_digest"]
        self.assertEqual(len(digest), 16)
        key_path = self.root / "location-digest.key"
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(key_path.read_bytes().hex(), raw)
        reloaded = SharedWorld(self.root, threads=threads, locations=locations, events=events)
        self.assertEqual(reloaded.sample(now=T0 + 602)["location_digest"], digest,
                         "重啟後要沿用同一把密鑰，不能把靜止誤判成移動")

    def test_location_digest_requires_secret_and_rejects_invalid_coordinates(self):
        row = {"lat": 35.68, "lon": 139.76}
        key = b"k" * 32
        digest = coarse_location_digest(row, key=key)
        self.assertEqual(len(digest), 16)
        self.assertEqual(digest, coarse_location_digest(row, key=key))
        self.assertNotEqual(digest, coarse_location_digest(row, key=b"x" * 32))
        self.assertEqual(coarse_location_digest(row), "", "沒有密鑰時禁止退回可列舉的裸 SHA-256")
        self.assertEqual(coarse_location_digest({"lat": 91, "lon": 0}, key=key), "")
        self.assertEqual(coarse_location_digest({"lat": "bad", "lon": 0}, key=key), "")
        self.assertEqual(coarse_location_digest(None, key=key), "")

    def test_cached_weather_uses_owner_ttl_and_persists_no_coordinates(self):
        locations = LocationStore(self.root / "locations")
        locations.save("guest", 1.0, 1.0, principal="guest")
        locations.save("owner", 35.68, 139.76, principal="owner")
        calls = []

        def fetch(lat, lon):
            calls.append((lat, lon))
            return {"current": {"weather_code": 1, "temperature_2m": 20.2}}

        now = [1000.0]
        cache = CachedWeather(locations, self.root / "weather.json", clock=lambda: now[0], fetch=fetch)
        self.assertEqual(cache.current()["text"], "晴れ、20℃")
        now[0] += 60
        cache.current()
        self.assertEqual(calls, [(35.68, 139.76)])
        raw = (self.root / "weather.json").read_text(encoding="utf-8")
        self.assertNotIn("35.68", raw)
        self.assertNotIn("139.76", raw)


class TestObserverBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.events = WorldEvents()

    def delta(self, entity="mira", **kwargs):
        return WorldDelta(self.root, entity, events=self.events, notes_root=None, **kwargs)

    def test_public_activity_excludes_private_self_and_repeats(self):
        log = ActivityLog(self.root / "logs")
        log.append("aster", kind="work", text="先にあった公開行動", visibility="public")
        delta = self.delta(activity_log=log, peers=("aster",))
        delta.observe(now=T0)
        log.append("aster", kind="work", text="新しい公開行動", visibility="public")
        log.append("aster", kind="secret", text="秘密の行動", visibility="private")
        log.append("mira", kind="work", text="自分の公開行動", visibility="public")
        first = delta.observe(now=T0 + 1)
        self.assertIn("新しい公開行動", first)
        self.assertNotIn("秘密", first)
        self.assertNotIn("自分の公開行動", first)
        self.assertNotIn("新しい公開行動", delta.block(now=T0 + 1))
        self.assertNotIn("新しい公開行動", delta.observe(now=T0 + 2, public=True))

    def test_game_scope_principal_entity_and_public_filter(self):
        self.events.publish("game_phase", "五目並べが始まった", entities=("mira",),
                            principals=("owner",))
        first = self.delta().observe(now=T0)
        self.assertIn("五目並べ", first)
        self.assertNotIn("五目並べ", self.delta("aster").observe(now=T0))
        self.assertNotIn("五目並べ", self.delta(principal="guest").observe(now=T0))
        self.assertNotIn("五目並べ", self.delta().observe(now=T0 + 1, public=True))

    def test_trpg_table_scope_and_voice_guild_scope(self):
        self.events.publish("trpg_phase", "TRPG が第 2 ラウンドに進んだ",
                            entities=("mira",), table_id="table-a")
        self.events.publish("voice_presence", "通話に誰かが入った",
                            entities=("mira",), guild_id="guild-a")
        joined = self.delta(guild_ids=("guild-a",), table_ids=("table-a",)).observe(now=T0)
        self.assertIn("第 2 ラウンド", joined)
        joined_next = self.delta(guild_ids=("guild-a",), table_ids=("table-a",)).observe(now=T0 + 1)
        self.assertIn("通話", joined_next)
        other_table = self.delta(guild_ids=("guild-a",), table_ids=("table-b",)).observe(now=T0)
        self.assertNotIn("TRPG", other_table)
        other_guild = self.delta(guild_ids=("guild-b",), table_ids=("table-a",)).observe(now=T0)
        self.assertNotIn("通話", other_guild)

    def test_missing_guild_never_publishes(self):
        from agent.adapter.discord_voice.presence import publish_voice_world_event
        source = types.SimpleNamespace(
            state=types.SimpleNamespace(app=types.SimpleNamespace(world_events=self.events)),
            config=types.SimpleNamespace(default_entity="mira"))
        publish_voice_world_event(source, guild_id="", is_bot=False,
                                  before_channel="", after_channel="room")
        self.assertNotIn("通話", self.delta(guild_ids=("guild-a",)).observe(now=T0))

    def test_truncated_event_is_retried_then_marked_seen(self):
        ledger = types.SimpleNamespace(items=lambda: [
            {"state": key} for key in ("pending", "hanging", "locked")])
        self.events.publish("environment", "あ" * 160, entities=("mira",), public=True)
        delta = self.delta(ledger=ledger, max_chars=80)
        complete_line = "・" + "あ" * 40

        first = delta.observe(now=T0)
        self.assertNotIn(complete_line, first, "第一窗被台帳差量擠掉，不能提前標記已讀")
        second = delta.observe(now=T0 + 1)
        self.assertIn(complete_line, second, "下一窗要完整補投這條事件")
        self.assertNotIn(complete_line, delta.observe(now=T0 + 2), "完整投過以後不能重複")


if __name__ == "__main__":
    unittest.main(verbosity=2)
