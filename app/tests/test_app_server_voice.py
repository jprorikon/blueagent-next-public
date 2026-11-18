#!/usr/bin/env python3
"""S4-g：App Server 实时语音会话、服务端切句、brain pipe 与内存事件。"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.config import AppConfig
from app.server.http import make_server
from app.server.social.models import BoardError
from app.server.voice import EnergyCutter, RATE


def pcm(ms: int, amplitude: int) -> bytes:
    count = RATE * ms // 1000
    return struct.pack("<" + "h" * count, *([amplitude] * count))


def frame(ms: int = 100, amplitude: int = 8000) -> str:
    return base64.b64encode(pcm(ms, amplitude)).decode("ascii")


class FakeBrain:
    def __init__(self) -> None:
        self.calls = []
        self.lock = threading.Lock()
        self.release = None
        #: `ring` 的回答（None＝接）；`legacy=True` 模擬不認得 `ring` 的舊 brain
        self.decline = None
        self.legacy = False

    def call(self, action: str, payload: dict) -> dict:
        with self.lock:
            self.calls.append((action, dict(payload)))
        if action == "ring":
            if self.legacy:
                raise BoardError(400, "bad_voice_action")
            if self.decline is not None:
                return {"ok": True, "answer": False, **self.decline}
            return {"ok": True, "answer": True, "reason": "free"}
        if action == "utterance" and self.release is not None:
            self.release.wait(3)
        if action != "utterance":
            return {"ok": True}
        return {"events": [
            {"name": "transcript", "payload": {"text": "おはよう"}},
            {"name": "speech", "payload": {"audio": "V0FW", "text": "おはよう"}},
            {"name": "done", "payload": {"text": "おはよう"}},
        ]}


class AppServerVoiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-app-voice-")
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        accounts = root / "accounts.json"
        rows = [("owner", "owner", "owner-token", None),
                ("mira", "entity", "mira-token", None),
                ("aster", "entity", "aster-token", None),
                ("guest", "guest", "guest-token", None)]
        accounts.write_text(json.dumps({"schema_version": 1, "accounts": [
            {"id": identity, "kind": kind,
             "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
             **({"visible_entities": ["mira"]} if identity == "owner" else {})}
            for identity, kind, token, _ in rows], "migration_accounts": []}), encoding="utf-8")
        self.server = make_server(AppConfig("127.0.0.1", 0, root / "data", accounts))
        self.brain = FakeBrain()
        self.server.services.voice.bridge = self.brain
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.thread.join, 2.0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, token="owner-token"):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else None

    def open_call(self):
        status, body = self.request("POST", "/v1/calls", {"entity": "mira", "language": "ja"})
        self.assertEqual(status, 200, body)
        call_id = body["session"]["id"]
        # 兩階段響鈴（2026-11-17）：先回 ringing，她的決定透過 /state 下發
        self.assertEqual(body["state"], "ringing")
        pickup = self.wait_state(call_id, "live")["pickup"]
        self.assertIn(pickup, {"free", "sleepy_free", "woken", "legacy"})   # brain 決定接的理由
        return call_id

    def wait_state(self, call_id, want, timeout=3.0):
        deadline = time.monotonic() + timeout
        body = {}
        while time.monotonic() < deadline:
            status, body = self.request("GET", f"/v1/calls/{call_id}/state")
            self.assertEqual(status, 200, body)
            if body["state"] == want:
                return body
            time.sleep(0.02)
        self.fail(f"call state timeout: want={want} got={body}")

    def wait_events(self, call_id, name, after=0):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, body = self.request("GET", f"/v1/calls/{call_id}/events?after={after}")
            self.assertEqual(status, 200, body)
            found = [event for event in body["events"] if event["name"] == name]
            if found:
                return found[-1], body["next_cursor"]
            time.sleep(0.02)
        self.fail("voice event timeout: " + name)

    def speak(self, call_id, seq=1):
        # 300 ms 有声 + 800 ms 静音：达到 160 ms 起句、700 ms 尾静音。
        status, body = self.request("POST", f"/v1/calls/{call_id}/frames",
                                    {"seq": seq, "audio": frame(300)})
        self.assertEqual(status, 200, body)
        for offset in range(8):
            status, body = self.request("POST", f"/v1/calls/{call_id}/frames",
                                        {"seq": seq + 1 + offset, "audio": frame(100, 0)})
            self.assertEqual(status, 200, body)
        return body

    def test_acl_frame_order_duplicate_and_late_audio(self):
        status, denied = self.request("POST", "/v1/calls", {"entity": "mira"}, token="guest-token")
        self.assertEqual((status, denied["error"]), (403, "forbidden"))
        status, denied = self.request("POST", "/v1/calls", {"entity": "aster"})
        self.assertEqual((status, denied["error"]), (403, "call_entity_forbidden"))
        call_id = self.open_call()
        status, body = self.request("GET", f"/v1/calls/{call_id}/events", token="mira-token")
        self.assertEqual((status, body["error"]), (404, "call_not_found"))
        status, body = self.request("POST", f"/v1/calls/{call_id}/frames",
                                    {"seq": 2, "audio": frame()})
        self.assertEqual((status, body["error"], body["expected"]), (409, "frame_out_of_order", 1))
        self.speak(call_id)
        status, duplicate = self.request("POST", f"/v1/calls/{call_id}/frames",
                                         {"seq": 2, "audio": frame(100, 0)})
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])
        utterance, _ = self.wait_events(call_id, "done")
        self.assertEqual(utterance["payload"]["text"], "おはよう")
        actions = [action for action, _ in self.brain.calls]
        self.assertEqual(actions.count("utterance"), 1)
        self.assertEqual(actions.count("ring"), 1)
        self.assertEqual(actions.count("interrupt"), 1)
        status, ended = self.request("POST", f"/v1/calls/{call_id}/hangup", {})
        self.assertEqual(status, 200, ended)
        status, late = self.request("POST", f"/v1/calls/{call_id}/frames",
                                    {"seq": 3, "audio": frame()})
        self.assertEqual((status, late["error"]), (410, "call_ended"))
        _, social = self.request("GET", "/v1/events")
        self.assertEqual(social["events"], [])

    def test_cutter_only_emits_complete_utterance_and_preroll(self):
        cutter = EnergyCutter()
        events = cutter.feed(pcm(300, 8000))
        self.assertEqual([name for name, _ in events], ["started"])
        self.assertFalse(any(name == "utterance" for name, _ in events))
        events = cutter.feed(pcm(800, 0))
        closed = [clip for name, clip in events if name == "utterance"]
        self.assertEqual(len(closed), 1)
        self.assertGreaterEqual(len(closed[0]), len(pcm(500, 0)))
        self.assertLessEqual(len(closed[0]), len(pcm(1100, 0)))

    def test_sse_exposes_matching_data_cursor_for_unity_resume(self):
        call_id = self.open_call()
        self.speak(call_id)
        self.wait_events(call_id, "done")
        request = urllib.request.Request(
            f"{self.base}/v1/calls/{call_id}/events?after=0",
            headers={"Authorization": "Bearer owner-token", "Accept": "text/event-stream"})
        received = []
        with urllib.request.urlopen(request, timeout=5) as response:
            event_name = ""
            event_id = ""
            while True:
                line = response.readline().decode("utf-8").strip()
                if line.startswith("event: "):
                    event_name = line[7:]
                elif line.startswith("id: "):
                    event_id = line[4:]
                elif line.startswith("data: ") and event_name != "ready":
                    payload = json.loads(line[6:])
                    self.assertEqual(str(payload["_seq"]), event_id)
                    received.append((event_name, payload["_seq"]))
                    if event_name == "done":
                        break
                event_name = "" if not line else event_name
                event_id = "" if not line else event_id
        self.assertEqual([name for name, _ in received if name == "done"], ["done"])
        self.assertEqual([seq for _, seq in received], sorted(set(seq for _, seq in received)))

    def test_ping_reaches_brain_and_stale_call_does_not_block_redial(self):
        call_id = self.open_call()
        status, _ = self.request("POST", f"/v1/calls/{call_id}/ping", {})
        self.assertEqual(status, 200)
        self.assertEqual(self.brain.calls[-1][0], "ping")
        self.server.services.voice._sessions[call_id].last_seen -= 46
        replacement = self.open_call()
        self.assertNotEqual(replacement, call_id)
        self.assertIn("hangup", [action for action, _ in self.brain.calls])
        status, ended = self.request("GET", f"/v1/calls/{call_id}/events?after=0")
        self.assertEqual((status, ended["state"]), (200, "ended"))
        self.assertEqual(ended["events"][-1]["payload"]["reason"], "idle")

    def test_barge_in_drops_old_epoch_audio(self):
        call_id = self.open_call()
        gate = threading.Event()
        self.brain.release = gate
        self.speak(call_id)
        deadline = time.monotonic() + 2
        while not any(action == "utterance" for action, _ in self.brain.calls):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        status, interrupted = self.request("POST", f"/v1/calls/{call_id}/interrupt", {})
        self.assertEqual(status, 200, interrupted)
        gate.set()
        time.sleep(0.15)
        _, page = self.request("GET", f"/v1/calls/{call_id}/events?after=0")
        self.assertFalse(any(event["name"] == "speech" for event in page["events"]))
        self.assertTrue(any(event["name"] == "interrupt" for event in page["events"]))

    def test_declined_ring_reports_declined_and_allows_redial(self):
        self.brain.decline = {"reason": "declined", "line": "いま手が離せない、あとでね"}
        status, body = self.request("POST", "/v1/calls", {"entity": "mira"})
        self.assertEqual((status, body["state"]), (200, "ringing"))
        call_id = body["session"]["id"]
        decided = self.wait_state(call_id, "declined")
        self.assertEqual((decided["reason"], decided["line"]), ("declined", "いま手が離せない、あとでね"))
        self.assertNotIn("start", [action for action, _ in self.brain.calls])
        self.brain.decline = {"reason": "dreaming"}          # 不認得的理由一律歸成 declined
        status, body = self.request("POST", "/v1/calls", {"entity": "mira"})
        self.assertEqual((status, body["state"]), (200, "ringing"))
        second = body["session"]["id"]
        self.assertEqual(self.wait_state(second, "declined")["line"], "")
        self.brain.decline = None
        status, body = self.request("POST", "/v1/calls", {"entity": "mira"})
        self.assertEqual((status, body["state"]), (200, "ringing"))
        self.assertEqual(self.wait_state(body["session"]["id"], "live")["pickup"], "free")

    def test_asleep_reason_passes_through(self):
        self.brain.decline = {"reason": "asleep"}
        status, body = self.request("POST", "/v1/calls", {"entity": "mira"})
        self.assertEqual((status, body["state"]), (200, "ringing"))
        decided = self.wait_state(body["session"]["id"], "declined")
        self.assertEqual(decided["reason"], "asleep")

    def test_legacy_brain_without_ring_still_connects(self):
        self.brain.legacy = True
        call_id = self.open_call()
        actions = [action for action, _ in self.brain.calls]
        self.assertEqual(actions[:2], ["ring", "start"])
        self.assertIn(call_id, self.server.services.voice._sessions)

    def test_second_dial_while_ringing_is_busy(self):
        entered, release = threading.Event(), threading.Event()
        original = self.brain.call

        def slow(action, payload):
            if action == "ring":
                entered.set()
                release.wait(3)
            return original(action, payload)

        self.brain.call = slow
        first = {}
        worker = threading.Thread(target=lambda: first.update(
            zip(("status", "body"), self.request("POST", "/v1/calls", {"entity": "mira"}))))
        worker.start()
        self.assertTrue(entered.wait(3))
        status, body = self.request("POST", "/v1/calls", {"entity": "mira"})
        self.assertEqual((status, body["error"]), (409, "busy_elsewhere"))
        release.set()
        worker.join(5)
        self.assertEqual(first["status"], 200, first)


if __name__ == "__main__":
    unittest.main()
