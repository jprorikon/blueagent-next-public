#!/usr/bin/env python3
"""心情 SSE：變更回調、按端投遞、權限隔離與重播。"""

from __future__ import annotations

import queue
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.client_end import MR, PHONE
from agent.brain.runtime.delivery.bus import EventBus
from agent.brain.runtime.inner.affect import AffectService
from agent.brain.runtime.inner.mood_events import MoodEvents
from agent.brain.runtime.delivery.outbox import TurnOutbox


class FakeAffect:
    def __init__(self):
        self.value = {"entity": "mira", "key": "calm", "valence": 0.0,
                      "arousal": 0.0, "social_need": 0.0, "idle_s": 0.0, "revision": 1}

    def mood(self, entity):
        return {**self.value, "entity": entity}


class TestMoodEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.outbox = TurnOutbox(self.tmp.name)
        self.bus = EventBus(recorder=self.outbox.record_delivery)
        self.affect = FakeAffect()
        self.publisher = MoodEvents(self.bus, self.affect, principal="owner")

    def tearDown(self):
        self.tmp.cleanup()

    def test_master_phone_and_mr_receive_typed_snapshots_but_guest_does_not(self):
        phone_id, phone = self.bus.subscribe("owner", PHONE)
        mr_id, mr = self.bus.subscribe("owner", MR)
        guest_id, guest = self.bus.subscribe("guest-x", PHONE)
        self.addCleanup(self.bus.unsubscribe, "owner", phone_id)
        self.addCleanup(self.bus.unsubscribe, "owner", mr_id)
        self.addCleanup(self.bus.unsubscribe, "guest-x", guest_id)

        self.assertEqual(self.publisher.publish("mira"), 2)
        for end, channel in ((PHONE, phone), (MR, mr)):
            frame = channel.get_nowait()
            self.assertEqual(frame["kind"], "mood")
            self.assertEqual(frame["entity"], "mira")
            self.assertEqual(frame["end"], end)
            self.assertEqual(frame["mood"]["key"], "calm")
        with self.assertRaises(queue.Empty):
            guest.get_nowait()
        self.assertEqual(self.outbox.since("", principal="owner"), [], "狀態事件不能進聊天 outbox")

    def test_identical_projection_is_deduplicated_and_changes_can_replay(self):
        sub_id, channel = self.bus.subscribe("owner", PHONE)
        self.addCleanup(self.bus.unsubscribe, "owner", sub_id)
        self.assertEqual(self.publisher.publish("mira"), 1)
        first = channel.get_nowait()
        self.assertEqual(self.publisher.publish("mira"), 0)
        with self.assertRaises(queue.Empty):
            channel.get_nowait()

        self.affect.value["valence"] = 0.4
        self.affect.value["key"] = "happy"
        self.assertEqual(self.publisher.publish("mira"), 1)
        second = channel.get_nowait()
        replayed = self.bus.replay("owner", first["id"], PHONE)
        self.assertEqual([row["id"] for row in replayed], [second["id"]])
        self.assertEqual(replayed[0]["mood"]["key"], "happy")

    def test_affect_notifies_for_heartbeat_touch_and_appraisal(self):
        changes = []
        service = AffectService(tempfile.mkdtemp(prefix="ba-mood-event-"), on_change=changes.append)
        service.advance("mira")
        self.assertEqual(changes, ["mira"])
        noted = service.note_message("mira", "こんにちは", speaker="owner")
        self.assertTrue(noted.get("ok"), noted)
        self.assertGreaterEqual(len(changes), 2, "touch 要更新寂寞度")
        result = service.appraise("mira", kind="pleasure", delta_v=0.2, delta_a=0.1,
                                  source_event_id=noted["event_id"])
        self.assertTrue(result.get("ok"), result)
        self.assertGreaterEqual(len(changes), 3)

    def test_bootstrap_connects_the_affect_callback(self):
        app = build(brain_delay=0, data_dir=tempfile.mkdtemp(prefix="ba-mood-wire-"),
                    step_duration=0.02, life_pause=0.005)
        self.addCleanup(app.shutdown_lives)
        self.assertIsNotNone(app.affect.on_change)


if __name__ == "__main__":
    unittest.main(verbosity=2)
