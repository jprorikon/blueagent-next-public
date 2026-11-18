#!/usr/bin/env python3
"""S4-g 跨进程：PCM → App Server 切句 → brain HTTP → 会话输出。"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from demo_common import *  # noqa: F401,F403
# 借用 App 測試裡的夾具（App Server 那一側的測試在 app/tests/）
import sys as _sys, pathlib as _pl  # noqa: E401
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "app" / "tests"))
from test_app_server_voice import frame  # noqa: E402
from test_app_voice_bridge import FakeSTT

from app.server.config import AppConfig
from app.server.http import make_server


class FakeTts:
    def speak(self, text, voice):
        class Clip:
            def payload(self):
                return {"text": text, "audio": "V0FW", "seconds": 0.5}
        return [Clip()]


class AppVoiceEndToEndCase(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.state.app.stt = FakeSTT("ミラ、おはよう。")
        self.state.app.voice.tts = FakeTts()
        # 牆鐘落在她的睡眠時段時她會不接（`runtime/inner/call_pickup.py`）；這條驗的是接通後的管線
        self.state.app.circadian = {}
        from agent.server import http as brain_http
        brain_http.set_state(self.state)
        self.brain = ThreadingHTTPServer(("127.0.0.1", 0), brain_http.Handler)
        threading.Thread(target=self.brain.serve_forever, daemon=True).start()
        self.addCleanup(self.brain.server_close)
        self.addCleanup(self.brain.shutdown)
        self.brain_url = f"http://127.0.0.1:{self.brain.server_address[1]}"

        self.tmp = tempfile.TemporaryDirectory(prefix="ba-voice-e2e-")
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        accounts = root / "accounts.json"
        accounts.write_text(json.dumps({"schema_version": 1, "accounts": [
            {"id": "owner", "kind": "owner", "legacy_principal": "owner",
             "token_sha256": hashlib.sha256(b"owner-token").hexdigest()},
            {"id": "mira", "kind": "entity",
             "token_sha256": hashlib.sha256(b"mira-token").hexdigest()},
        ], "migration_accounts": []}), encoding="utf-8")
        self.app = make_server(AppConfig(
            "127.0.0.1", 0, root / "data", accounts,
            brain_url=self.brain_url, voice_token="shared-secret"))
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)
        self.base = f"http://127.0.0.1:{self.app.server_port}"
        self.secret = mock.patch.dict(os.environ, {"BLUEAGENT_APP_VOICE_TOKEN": "shared-secret"})
        self.secret.start()
        self.addCleanup(self.secret.stop)

    def post(self, path, body):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer owner-token", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())

    def get(self, path):
        request = urllib.request.Request(self.base + path, headers={"Authorization": "Bearer owner-token"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())

    def wait_live(self, call_id, timeout=10.0):
        """兩階段響鈴（d8a308c）：`POST /v1/calls` 先回 ringing，她接了才轉 live——響鈴期間送幀是 410。"""
        deadline = time.monotonic() + timeout
        state = {}
        while time.monotonic() < deadline:
            state = self.get(f"/v1/calls/{call_id}/state")
            if state.get("state") != "ringing":
                break
            time.sleep(0.05)
        self.assertEqual(state.get("state"), "live", state)

    def test_real_http_pipe_delivers_transcript_and_speech_without_social_event(self):
        opened = self.post("/v1/calls", {"entity": "mira", "language": "ja"})
        call_id = opened["session"]["id"]
        self.addCleanup(self.post, f"/v1/calls/{call_id}/hangup", {})
        self.assertEqual(opened.get("state"), "ringing")
        self.wait_live(call_id)
        self.post(f"/v1/calls/{call_id}/frames", {"seq": 1, "audio": frame(300)})
        for seq in range(2, 10):
            self.post(f"/v1/calls/{call_id}/frames", {"seq": seq, "audio": frame(100, 0)})
        deadline = time.monotonic() + 10
        seen = []
        cursor = 0
        while time.monotonic() < deadline and "done" not in seen:
            request = urllib.request.Request(
                self.base + f"/v1/calls/{call_id}/events?after={cursor}",
                headers={"Authorization": "Bearer owner-token"})
            with urllib.request.urlopen(request, timeout=5) as response:
                page = json.loads(response.read())
            for event in page["events"]:
                seen.append(event["name"])
                if event["name"] == "speech":
                    # 保持旧 Unity 合同：event=payload，真正的音频在内层 payload。
                    self.assertTrue(event["payload"]["payload"]["audio"])
                if event["name"] == "transcript":
                    self.assertEqual(event["payload"]["text"], "ミラ、おはよう。")
            cursor = page["next_cursor"]
            if "done" not in seen:
                time.sleep(0.05)
        self.assertIn("transcript", seen)
        self.assertIn("speech", seen)
        self.assertIn("done", seen)
        request = urllib.request.Request(self.base + "/v1/events",
                                         headers={"Authorization": "Bearer owner-token"})
        with urllib.request.urlopen(request, timeout=5) as response:
            social = json.loads(response.read())
        self.assertEqual(social["events"], [])


if __name__ == "__main__":
    unittest.main()
