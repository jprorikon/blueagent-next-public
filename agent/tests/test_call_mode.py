#!/usr/bin/env python3
"""App 的單人通話（owner 2026-10-29：「實時和 agent 對話」、只給 master、可以打斷）。

判據：
  ① `POST /api/voice/call` 拿一通 `end=phone`：冪等；她在 Discord 語音房裡 ⇒ 409，不搶嘴；
  ② App 掉線（沒有心跳）超過 `call_idle_s` ⇒ 自動掛、發言權放掉；
  ③ `X-Call` 的語音：只認主人、只認現在那一通；來源標 `phone:call`（實時源），掛了 ⇒ 410；
  ④ 通話中的回合 prompt 帶 `[[call]]`（講短、講口語）；
  ⑤ `interrupt` ⇒ 世代 +1（這之前開始的合成都作廢）；
  ⑥ 整條：通話中送一句 ⇒ 沒要 want_voice 也出聲（粘性），掛斷之後不再出聲。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.turns import source_class_for
from agent.brain.messages import build_messages
from agent.brain.domain.auth import AuthContext, Role
from agent.brain.domain.event import SourceClass
from agent.brain.ports import BrainRequest
from agent.server import api as api_module
from test_audio_input import FakeSTT, wav_bytes

OWNER = AuthContext(principal="owner", role=Role.OWNER, capabilities=frozenset({"chat"}))
GUEST = AuthContext(principal="guest-x", role=Role.GUEST, capabilities=frozenset({"chat"}))


class CallCase(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.voice = self.state.app.voice

    def post(self, action, body=None, auth=OWNER):
        return api_module.dispatch_post(self.state, f"/api/voice/{action}",
                                        dict(body or {"entity": "mira"}), auth=auth)


class TestCallLifecycle(CallCase):
    def test_call_ping_hangup(self):
        status, data = self.post("call")
        self.assertEqual(status, 200, data)
        session = data["session"]
        self.assertEqual(session["end"], "phone")
        self.assertEqual(self.voice.floor.holder("mira"), session["id"])

        status, again = self.post("call")
        self.assertEqual(status, 200)
        self.assertTrue(again["existing"])
        self.assertEqual(again["session"]["id"], session["id"])

        status, _ = self.post("ping", {"entity": "mira", "call_id": session["id"]})
        self.assertEqual(status, 200)
        status, data = self.post("ping", {"entity": "mira", "call_id": "call_other"})
        self.assertEqual(status, 410)

        status, _ = self.post("hangup")
        self.assertEqual(status, 200)
        self.assertIsNone(self.voice.active("mira"))
        self.assertIsNone(self.voice.floor.holder("mira"))
        status, _ = self.post("ping", {"entity": "mira", "call_id": session["id"]})
        self.assertEqual(status, 410)

    def test_busy_in_discord_voice_room(self):
        self.voice.call("mira", end="discord", principal="owner")
        status, data = self.post("call")
        self.assertEqual(status, 409)
        self.assertEqual(data["error"], "busy_elsewhere")

    def test_guest_cannot_call(self):
        status, _ = self.post("call", auth=GUEST)
        self.assertEqual(status, 403)

    def test_unknown_entity(self):
        status, _ = self.post("call", {"entity": "nobody"})
        self.assertEqual(status, 400)

    def test_hangup_is_per_entity(self):
        self.post("call", {"entity": "aster"})
        self.post("hangup", {"entity": "mira"})
        self.assertIsNotNone(self.voice.active("aster"))
        self.post("hangup", {"entity": "aster"})
        self.assertIsNone(self.voice.active("aster"))

    def test_silent_app_is_hung_up(self):
        _, data = self.post("call")
        session = self.voice.active("mira")
        session.last_seen = time.time() - self.voice.call_idle_s - 1
        self.assertIsNone(self.voice.active("mira"))
        self.assertIsNone(self.voice.floor.holder("mira"))

    def test_discord_session_never_idles_out(self):
        self.voice.call("mira", end="discord", principal="owner")
        self.voice._sessions["mira"].last_seen = 0.0
        self.assertIsNotNone(self.voice.active("mira"))

    def test_interrupt_bumps_epoch_and_preserves_call_floor(self):
        _, call = self.post("call")
        call_id = call["session"]["id"]
        before = self.voice.epoch("mira")
        status, data = self.post("interrupt")
        self.assertEqual(status, 200)
        self.assertEqual(data["epoch"], before + 1)
        self.assertEqual(data["reason"], "app_barge_in")
        self.assertIsNotNone(self.voice.active("mira"))
        self.assertEqual(self.voice.floor.holder("mira"), call_id)
        self.assertFalse(self.voice.floor.acquire("mira", "other-turn", priority=5, timeout=0))

    def test_stale_hangup_cannot_end_replacement_call(self):
        _, first = self.post("call")
        old_id = first["session"]["id"]
        status, _ = self.post("hangup", {"entity": "mira", "call_id": old_id})
        self.assertEqual(status, 200)
        _, second = self.post("call")
        new_id = second["session"]["id"]
        self.assertNotEqual(new_id, old_id)

        status, data = self.post("hangup", {"entity": "mira", "call_id": old_id})
        self.assertEqual(status, 410)
        self.assertEqual(data["error"], "call_ended")
        self.assertEqual(self.voice.active("mira").id, new_id)
        self.assertEqual(self.voice.floor.holder("mira"), new_id)

        status, _ = self.post("hangup", {"entity": "mira", "call_id": new_id})
        self.assertEqual(status, 200)
        self.assertIsNone(self.voice.active("mira"))


class TestCallAudio(CallCase):
    def setUp(self):
        super().setUp()
        self.state.app.stt = FakeSTT()

    def prepare(self, auth=OWNER, **headers):
        return api_module.prepare_audio(self.state, wav_bytes(), headers=headers, auth=auth)

    def test_call_audio_is_tagged_and_realtime(self):
        _, data = self.post("call")
        call_id = data["session"]["id"]
        status, data = self.prepare(**{"X-Call": call_id, "X-Entity": "mira"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["source_override"], "phone:call")
        self.assertEqual(data["call"], call_id)
        self.assertIs(source_class_for("phone:call"), SourceClass.REALTIME)

    def test_plain_voice_message_is_unchanged(self):
        _, data = self.prepare()
        self.assertEqual(data["source_override"], "api:audio")
        self.assertEqual(data["call"], "")

    def test_ended_call_is_410(self):
        status, data = self.prepare(**{"X-Call": "call_gone", "X-Entity": "mira"})
        self.assertEqual(status, 410)
        self.assertEqual(data["error"], "call_ended")

    def test_guest_cannot_use_call_header(self):
        _, data = self.post("call")
        status, _ = self.prepare(auth=GUEST, **{"X-Call": data["session"]["id"]})
        self.assertEqual(status, 403)


class TestCallTurn(CallCase):
    def test_prompt_knows_it_is_a_call(self):
        request = BrainRequest(thread_id="th", entity="mira", principal="owner",
                               text="もしもし", system="", mode="work",
                               meta={"in_call": True, "transcript": []})
        joined = "\n".join(str(row["content"]) for row in build_messages(request))
        self.assertIn("[[call]]", joined)
        plain = BrainRequest(thread_id="th", entity="mira", principal="owner",
                             text="もしもし", system="", mode="work", meta={"transcript": []})
        self.assertNotIn("[[call]]",
                         "\n".join(str(row["content"]) for row in build_messages(plain)))

    def test_brain_sees_in_call_flag(self):
        seen = []

        class Spy:
            def run_turn(self, request):
                seen.append(bool(request.meta.get("in_call")))
                yield ("done", {"text": "うん。", "entity": "mira", "principal": "owner",
                                "model": "test"})

        self.state.app.runtime.ports.brain = Spy()
        self.state.send("もしもし", entity="mira", principal="owner", mode="work",
                        source_override="phone:call")
        self.state.send("もしもし", entity="mira", principal="owner", mode="work")
        self.assertEqual(seen, [True, False])

    def test_replies_are_spoken_only_while_in_call(self):
        def speech_count():
            frames = [payload for kind, payload in self.state.send_stream(
                text="今日はどうだった？", entity="mira", principal="owner", mode="work",
                source_override="phone:call") if kind == "frame"]
            return sum(1 for frame in frames if frame.get("kind") == "speech")

        self.post("call")
        self.assertGreater(speech_count(), 0, "通話中沒要 want_voice 也要出聲")
        self.post("hangup")
        self.assertEqual(speech_count(), 0)


class TestCallHttp(CallCase):
    """HTTP 層：`X-Call` 要穿過 `_audio`，而且通話中**不走**「先短回、後交付」那條。"""

    def setUp(self):
        super().setUp()
        self.state.app.stt = FakeSTT()
        from agent.server import http as app_server
        app_server.set_state(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def request(self, path, data, headers):
        request = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Authorization": f"Bearer {self.state.app.auth.owner_token}", **headers})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_call_turn_streams_with_voice(self):
        status, raw = self.request("/api/voice/call", json.dumps({"entity": "mira"}).encode(),
                                   {"Content-Type": "application/json"})
        self.assertEqual(status, 200, raw)
        call_id = json.loads(raw)["session"]["id"]
        status, raw = self.request("/api/audio", wav_bytes(),
                                   {"Content-Type": "audio/wav", "X-Call": call_id,
                                    "X-Entity": "mira"})
        self.assertEqual(status, 200, raw)
        events = [block.split("\n")[0] for block in raw.split("\n\n") if block.strip()]
        self.assertTrue(events[0].startswith("event: transcript"), events[0])
        self.assertIn("event: accepted", raw, "通話走串流（有 turn_id，outbox 可補）")
        self.assertIn("event: speech", raw)
        self.assertNotIn('"following_up": true', raw)

    def test_hung_up_call_is_410(self):
        status, _ = self.request("/api/audio", wav_bytes(),
                                 {"Content-Type": "audio/wav", "X-Call": "call_gone"})
        self.assertEqual(status, 410)


if __name__ == "__main__":
    unittest.main(verbosity=2)
