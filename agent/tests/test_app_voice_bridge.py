#!/usr/bin/env python3
"""S4-g：brain 语音 pipe 的会话绑定、STT、TTS 输出与共享密钥。"""

from __future__ import annotations

import base64
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.adapter import app_voice
from agent.adapter.stt_local import Transcript


class FakeSTT:
    def __init__(self, text="おはよう、オーナー。") -> None:
        self.text = text
        self.calls = []

    def transcribe(self, audio, *, language="", suffix=".wav"):
        self.calls.append({"audio": audio, "language": language, "suffix": suffix})
        return Transcript(text=self.text, language=language or "ja", probability=0.98,
                          seconds=1.0, engine="fake")


def payload(call_id="call_platform_1"):
    return {"call_id": call_id, "account_id": "acct_owner", "principal": "owner",
            "entity": "mira", "language": "ja", "epoch": 0}


def utterance(call_id="call_platform_1"):
    return {**payload(call_id), "action": "utterance", "utterance_id": call_id + ":1",
            "format": "wav", "audio": base64.b64encode(b"RIFF" + b"\0" * 400).decode()}


class AppVoiceBridgeCase(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.stt = FakeSTT()
        self.state.app.stt = self.stt

    def start(self):
        status, reply = app_voice.handle(self.state, {**payload(), "action": "start"})
        self.assertEqual(status, 200, reply)
        return reply

    def test_start_stt_stream_speech_and_hangup_keep_platform_id(self):
        started = self.start()
        self.assertEqual(started["session"]["id"], "call_platform_1")
        self.assertEqual(started["session"]["target"], "app:call_platform_1")
        status, reply = app_voice.handle(self.state, utterance())
        self.assertEqual(status, 200, reply)
        names = [event["name"] for event in reply["events"]]
        self.assertEqual(names[0], "transcript")
        self.assertIn("speech", names)
        self.assertIn("done", names)
        self.assertEqual(self.stt.calls[0]["language"], "ja")
        speech = next(event["payload"]["payload"] for event in reply["events"]
                      if event["name"] == "speech")
        self.assertEqual(speech["call_id"], "call_platform_1")
        self.assertEqual(speech["target"], "app:call_platform_1")
        status, _ = app_voice.handle(self.state, {**payload(), "action": "hangup"})
        self.assertEqual(status, 200)
        self.assertIsNone(self.state.app.voice.active("mira"))
        status, again = app_voice.handle(self.state, {**payload(), "action": "hangup"})
        self.assertEqual(status, 200, again)

    def test_ping_keeps_the_same_platform_session_active(self):
        self.start()
        status, reply = app_voice.handle(self.state, {**payload(), "action": "ping"})
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["session"]["id"], "call_platform_1")
        self.assertIsNotNone(self.state.app.voice.active("mira"))

    def test_wrong_call_never_reaches_stt_or_turn(self):
        self.start()
        status, reply = app_voice.handle(self.state, utterance("call_other"))
        self.assertEqual((status, reply["error"]), (410, "call_ended"))
        self.assertEqual(self.stt.calls, [])
        status, reply = app_voice.handle(self.state, {**payload(), "action": "interrupt"})
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["epoch"], 1)

    def test_stt_failure_does_not_invent_text(self):
        self.start()
        self.stt.text = ""
        status, reply = app_voice.handle(self.state, utterance())
        self.assertEqual((status, reply["error"]), (422, "stt_failed"))

    def test_busy_other_channel_is_not_taken_over_silently(self):
        original = self.state.app.voice.call("mira", end="phone", principal="owner")
        self.assertTrue(original["ok"])
        status, reply = app_voice.handle(self.state, {**payload(), "action": "start"})
        self.assertEqual((status, reply["error"]), (409, "busy_elsewhere"))


class AppVoiceHttpCase(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.state.app.stt = FakeSTT()
        from agent.server import http as brain_http
        brain_http.set_state(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), brain_http.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def post(self, body, token="shared-secret"):
        request = urllib.request.Request(
            self.base + "/internal/app/voice", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-App-Voice-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_shared_secret_is_mandatory_and_not_user_bearer_auth(self):
        with mock.patch.dict(os.environ, {"BLUEAGENT_APP_VOICE_TOKEN": "shared-secret"}):
            status, denied = self.post({**payload(), "action": "start"}, token="wrong")
            self.assertEqual((status, denied["error"]), (403, "forbidden"))
            self.assertIsNone(self.state.app.voice.active("mira"))
            status, started = self.post({**payload(), "action": "start"})
            self.assertEqual(status, 200, started)
            status, reply = self.post(utterance())
            self.assertEqual(status, 200, reply)
            self.assertEqual(reply["events"][0]["name"], "transcript")

    def test_unconfigured_pipe_is_closed(self):
        with mock.patch.dict(os.environ, {"BLUEAGENT_APP_VOICE_TOKEN": ""}):
            status, reply = self.post({**payload(), "action": "start"})
            self.assertEqual((status, reply["error"]), (503, "app_voice_unconfigured"))


if __name__ == "__main__":
    unittest.main()
