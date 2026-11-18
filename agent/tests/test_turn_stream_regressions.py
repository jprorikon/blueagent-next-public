#!/usr/bin/env python3
"""`/api/turn`／`/api/stream`／`/api/turns` 的邊界回歸（2026-11-04）。"""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

import agent.server.api as api_module
from agent.adapter.stt_local import Transcript
from agent.brain.compose.streaming import StreamDelivery
from agent.brain.domain.auth import AuthContext, Role
from agent.brain.runtime.delivery.outbox import TurnOutbox


OWNER = AuthContext(principal="owner", role=Role.OWNER, capabilities=frozenset({"chat"}))
GUEST = AuthContext(principal="guest-x", role=Role.GUEST, capabilities=frozenset({"chat"}))


def _accepted(events):
    for kind, payload in events:
        if kind == "frame" and payload.get("kind") == "accepted":
            return str(payload["payload"]["turn_id"])
    raise AssertionError(f"missing accepted frame: {events}")


class TestStreamPersistenceRegressions(unittest.TestCase):
    def setUp(self):
        self.box = TurnOutbox(pathlib.Path(tempfile.mkdtemp()), keep=20)
        self.delivery = StreamDelivery(send=lambda **_: {}, outbox=self.box)
        self.kwargs = {"principal": "owner", "entity": "mira",
                       "source_override": "phone:owner"}

    def test_late_parts_are_merged_into_the_timed_out_turn_on_recovery(self):
        self.box.note_part("t-late", "先に言った一句。", meta=self.kwargs)
        self.delivery.save("t-late", {"ok": False, "finished": False,
                                      "error": "TimeoutError: pending",
                                      "source": "phone:owner", "decision": {"thread": "th-1"}},
                           [], {"frames": 1, "speech": 0}, self.kwargs)
        self.box.note_part("t-late", "タイムアウト後のもう一句。")

        self.assertEqual(self.box.recover_pending(), 1)
        row = self.box.get("t-late", principal="owner")
        self.assertEqual(row["text"], "先に言った一句。タイムアウト後のもう一句。")
        self.assertEqual(row["parts"], ["先に言った一句。", "タイムアウト後のもう一句。"])
        self.assertEqual(row["thread"], "th-1")
        self.assertFalse(row["ok"])
        self.assertFalse(self.box.parts_path("t-late").exists())
        self.assertEqual(self.box.recover_pending(), 0)
        self.assertEqual(self.box.get("t-late")["text"], row["text"], "重跑不能重複追加")

    def test_real_decision_thread_is_written(self):
        self.delivery.save("t-thread", {"ok": True, "finished": True, "text": "はい",
                                        "source": "phone:owner", "decision": {"thread": "th-real"}},
                           [], {}, self.kwargs, reply="はい")
        self.assertEqual(self.box.get("t-thread")["thread"], "th-real")

    def test_duplicate_retry_reuses_turn_id_and_does_not_write_an_empty_row(self):
        calls = {"count": 0}

        def send(**_kwargs):
            calls["count"] += 1
            if calls["count"] > 1:
                return {"ok": True, "finished": True, "duplicate": True,
                        "source": "phone:owner", "decision": None, "frames": []}
            return {"ok": True, "finished": True, "source": "phone:owner",
                    "decision": {"thread": "th-original"},
                    "frames": [{"kind": "done", "payload": {"text": "原本の返事"}}]}

        delivery = StreamDelivery(send=send, outbox=self.box)
        first = list(delivery.stream(text="同じ要求", principal="owner", entity="mira",
                                     request_id="request-1", source_override="phone:owner"))
        second = list(delivery.stream(text="同じ要求", principal="owner", entity="mira",
                                      request_id="request-1", source_override="phone:owner"))
        self.assertEqual(_accepted(first), _accepted(second))
        rows = self.box.since("", principal="owner")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["text"], "原本の返事")
        self.assertEqual(rows[0]["turn_id"], _accepted(first))


class TestStreamErrorAndSourceRegressions(unittest.TestCase):
    def test_exception_is_not_recorded_as_success(self):
        outbox = mock.Mock()

        def send(**_kwargs):
            raise RuntimeError("boom")

        events = list(StreamDelivery(send=send, outbox=outbox).stream(text="x", principal="owner"))
        self.assertEqual(events[-1][0], "error")
        recorded = outbox.record.call_args.kwargs
        self.assertFalse(recorded["ok"])
        self.assertIn("RuntimeError: boom", recorded["error"])
        self.assertEqual(recorded["text"], "")


class TestHttpSourceAndStatsRegressions(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)

    def test_guest_cannot_forge_a_call_or_discord_source(self):
        for source in ("discord:vc", "phone:call", "mr:call"):
            status, data = api_module.prepare_send(
                self.state, {"text": "こんにちは", "entity": "mira", "source": source},
                auth=GUEST)
            self.assertEqual(status, 200, data)
            self.assertEqual(data["source_override"], "")

    def test_mr_text_turn_uses_the_mr_source(self):
        status, data = api_module.prepare_send(
            self.state, {"text": "こんにちは", "entity": "mira"}, auth=OWNER, source="mr")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["source_override"], "mr:owner")

    def test_server_derived_guest_audio_source_is_still_trusted(self):
        class FakeSTT:
            def transcribe(self, audio, *, language="", suffix=".wav"):
                return Transcript(text="写真を送ったよ。", language="ja", probability=0.95,
                                  seconds=1.0, engine="fake")

        self.state.app.stt = FakeSTT()
        status, data = api_module.prepare_audio(
            self.state, b"RIFF\x00\x00", headers={"X-Entity": "mira"}, auth=GUEST)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["source_override"], "api:audio")

    def test_aggregated_items_cannot_bypass_text_length_limit(self):
        status, data = api_module.prepare_send(
            self.state,
            {"entity": "mira", "items": [
                {"kind": "text", "text": "あ" * 600},
                {"kind": "text", "text": "い" * 600},
            ]}, auth=OWNER)
        self.assertEqual(status, 400, data)
        self.assertEqual(data, {"error": "text too long"})

    def test_guest_stats_only_cover_their_own_turns(self):
        self.state.app.outbox.record(turn_id="t-owner", principal="owner", entity="mira",
                                     text="owner")
        self.state.app.outbox.record(turn_id="t-guest", principal="guest-x", entity="mira",
                                     text="guest")
        status, guest = api_module.list_turns(self.state, {}, auth=GUEST)
        self.assertEqual(status, 200)
        self.assertEqual(guest["stats"]["turns"], 1)
        self.assertNotIn("recorded", guest["stats"], "全域計數不能給 guest")
        status, owner = api_module.list_turns(self.state, {}, auth=OWNER)
        self.assertEqual(status, 200)
        self.assertEqual(owner["stats"]["turns"], 2)
        self.assertIn("recorded", owner["stats"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
