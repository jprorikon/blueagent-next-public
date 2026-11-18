#!/usr/bin/env python3
"""App 側契約：**只走 SSE**（`docs/APP-API-SSE.md`）。

判據：
  ① `POST /api/turn` 回 `text/event-stream`，事件有序、`done` 收尾；
  ② `GET /api/events` 是常駐推送：先 `ready`，之後 `delivery`（含心跳）；
  ③ 語音 + work ⇒ 短回覆在請求那條線上、完整答案在 `delivery`（`result.following_up=true`）；
  ④ `/api/health` 帶契約版本 `api: sse-v1`。
"""

from __future__ import annotations

import json
import queue
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.state import AppState
from agent.brain.runtime.delivery.bus import EventBus


class TestEventBus(unittest.TestCase):
    def test_publish_reaches_all_subscriptions_of_the_principal(self):
        bus = EventBus()
        first_id, first = bus.subscribe("owner")
        second_id, second = bus.subscribe("owner")
        other_id, other = bus.subscribe("guest-x")
        try:
            self.assertEqual(bus.publish("owner", {"kind": "delivery", "text": "hi"}), 2)
            self.assertEqual(first.get_nowait()["text"], "hi")
            self.assertEqual(second.get_nowait()["text"], "hi")
            with self.assertRaises(queue.Empty):
                other.get_nowait()
        finally:
            bus.unsubscribe("owner", first_id)
            bus.unsubscribe("owner", second_id)
            bus.unsubscribe("guest-x", other_id)

    def test_queue_is_bounded_and_drops_oldest(self):
        bus = EventBus(queue_max=8)
        sub_id, channel = bus.subscribe("owner")
        try:
            for index in range(20):
                bus.publish("owner", {"kind": "delivery", "text": str(index)})
            self.assertEqual(channel.qsize(), 8)
            self.assertEqual(channel.get_nowait()["text"], "12", "應該丢最舊的")
        finally:
            bus.unsubscribe("owner", sub_id)


class TestSseReplayRace(unittest.TestCase):
    def test_live_copy_of_replayed_event_is_not_sent_twice(self):
        import io
        import types
        from unittest import mock
        from agent.server import http as http_module

        duplicate = {"id": 1000001, "kind": "delivery", "text": "補洞時也進了即時佇列"}
        fresh = {"id": 1000002, "kind": "delivery", "text": "真正的新事件"}

        class StopOnPing(io.BytesIO):
            def write(self, data):
                if data == b": ping\n\n":
                    raise BrokenPipeError()
                return super().write(data)

        channel = mock.Mock()
        channel.get.side_effect = [duplicate, fresh, queue.Empty()]
        bus = mock.Mock()
        bus.subscribe.return_value = ("sub-1", channel)
        bus.replay.return_value = [duplicate]
        handler = object.__new__(http_module.Handler)
        handler._state = lambda: types.SimpleNamespace(app=types.SimpleNamespace(bus=bus))
        handler.path = "/api/events"
        handler.headers = {}
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = StopOnPing()

        handler._events(auth=types.SimpleNamespace(principal="owner"))
        payload = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(payload.count("id: 1000001\n"), 1, payload)
        self.assertEqual(payload.count("id: 1000002\n"), 1, payload)
        bus.unsubscribe.assert_called_once_with("owner", "sub-1")


class TestSseContract(unittest.TestCase):
    def setUp(self):
        from agent.server import api as _api
        from agent.server import http as http_module
        self.app = build(brain_delay=0, data_dir=tempfile.mkdtemp(prefix="ba-sse-"),
                          step_duration=0.02, life_pause=0.005)
        self.state = AppState(self.app)
        http_module.STATE = self.state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_module.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.token = self.app.auth.owner_token or ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.shutdown_lives()

    # ── 工具 ────────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        auth = self.token or self._recover_token()
        return {"Authorization": f"Bearer {auth}", "Content-Type": "application/json"}

    def _recover_token(self) -> str:
        """只存 hash 之後，測試裡要用「重建前拿到的那把」——這裡直接輪替一把新的。"""
        self.token = self.app.auth.rotate()
        return self.token

    def _read_sse(self, response, *, stop_after: int = 200) -> list[tuple[str, dict | str]]:
        events: list[tuple[str, dict | str]] = []
        name = ""
        for raw in response:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = line.split(":", 1)[1].strip()
                try:
                    events.append((name, json.loads(payload)))
                except json.JSONDecodeError:
                    events.append((name, payload))
            elif line == "":
                if len(events) >= stop_after:
                    break
        return events

    # ── 判據 ────────────────────────────────────────────────────────────────
    def test_health_is_public_but_counts_require_owner(self):
        response = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=5)
        public = json.loads(response.read())
        self.assertEqual(public["api"], "sse-v1")
        self.assertNotIn("counts", public, "公開探活不能洩漏記憶庫與工作負載規模")

        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/health",
                                         headers=self._headers())
        owner = json.loads(urllib.request.urlopen(request, timeout=5).read())
        self.assertEqual(owner["api"], "sse-v1")
        self.assertIn("threads", owner["counts"])

    def test_turn_returns_sse_and_ends_with_done(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/turn",
            data=json.dumps({"text": "おはよう", "entity": "mira"}).encode(),
            headers=self._headers())
        events = self._read_sse(urllib.request.urlopen(request, timeout=30))
        self.assertTrue(events, events)
        names = [name for name, _payload in events]
        self.assertIn("done", names, names)
        # `done` 是語意上的結束（final=true）；後面還有一條 `result`（thread/observed 等元資料）
        self.assertEqual(names[-1], "result", names)
        self.assertLess(names.index("done"), names.index("result"))

    def test_parts_profile_reconstructs_text_without_done_duplicate(self):
        headers = self._headers()
        headers["X-Stream-Profile"] = "parts-v1"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/turn",
            data=json.dumps({"text": "おはよう", "entity": "mira"}).encode(),
            headers=headers)
        events = self._read_sse(urllib.request.urlopen(request, timeout=30))
        parts = [(payload.get("payload") or payload)["text"]
                 for name, payload in events if name == "part"]
        done = next(payload for name, payload in events if name == "done")["payload"]
        self.assertTrue(parts, events)
        self.assertNotIn("text", done)
        self.assertNotIn("bubbles", done)
        self.assertEqual(done["stream_profile"], "parts-v1")
        self.assertTrue(done["text_hash"])

    def test_events_stream_starts_with_ready(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/events",
                                         headers={"Authorization": f"Bearer {self._recover_token()}"})
        response = urllib.request.urlopen(request, timeout=10)
        try:
            line = response.readline().decode()
            self.assertEqual(line.strip(), "event: ready")
        finally:
            response.close()

    def test_mood_replay_keeps_its_event_type_and_snapshot(self):
        # 第一次連線的游標 0 本來就不重播；先留一條基線才能驗證補洞。
        self.app.bus.publish("owner", {"kind": "delivery", "text": "baseline"})
        cursor = self.app.bus.last_id("owner")
        self.app.bus.publish("owner", {
            "kind": "mood", "entity": "mira", "end": "phone",
            "mood": {"key": "happy", "valence": 0.4, "arousal": 0.2,
                     "social_need": 0.0, "revision": 7},
        }, end="phone")
        replay = self.app.bus.replay("owner", cursor, "phone")
        self.assertEqual([row["kind"] for row in replay], ["mood"], replay)
        token = self._recover_token()
        auth = self.app.auth.authenticate("Bearer " + token)
        self.assertEqual(auth.principal, "owner")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/events",
            headers={"Authorization": f"Bearer {token}",
                     "Last-Event-ID": str(cursor)})
        response = urllib.request.urlopen(request, timeout=10)
        try:
            self.assertEqual(response.readline().decode().strip(), "event: ready")
            response.readline()
            response.readline()
            events = self._read_sse(response, stop_after=1)
            self.assertEqual(events[0][0], "mood", events)
            self.assertEqual(events[0][1]["entity"], "mira")
            self.assertEqual(events[0][1]["mood"]["revision"], 7)
        finally:
            response.close()

    def test_delivery_carries_the_followup_text(self):
        """後續交付**必須有文字**（踩過：結果裡沒有頂層 reply，送出空字串）。"""
        result = self.state.send("用语音说话", principal="owner", want_voice=True)
        self.assertTrue(self.state.reply_text(result), "reply_text 要取得到全文")
        sent: list[dict] = []
        self.app.bus.publish = lambda principal, frame: sent.append(frame) or 1
        self.state.send_voice("何か作業を頼む", principal="owner", mode="work",
                              source="discord:vc")
        import time as _time
        for _ in range(60):
            if sent:
                break
            _time.sleep(0.1)
        self.assertTrue(sent, "應該要有一次 delivery")
        self.assertTrue(sent[0]["text"], f"delivery 不該是空的：{sent[0]}")

    def test_delivery_reaches_the_events_stream(self):
        token = self._recover_token()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/events",
                                         headers={"Authorization": f"Bearer {token}"})
        response = urllib.request.urlopen(request, timeout=10)
        try:
            # ready 事件三行：event / data / 空行 —— 要整段吃掉，否則下一段解析會多一個空事件
            self.assertEqual(response.readline().decode().strip(), "event: ready")
            response.readline()
            response.readline()
            time.sleep(0.1)
            self.app.bus.publish("owner", {"kind": "delivery", "text": "出來了出來了"})
            # 這條線上可能先來「她自己活動」的主動訊息 ⇒ 要找到我們發的那一條，不是第一條
            found = None
            for _ in range(6):
                events = self._read_sse(response, stop_after=1)
                if not events:
                    break
                if events[0][1].get("text") == "出來了出來了":
                    found = events[0]
                    break
            self.assertIsNotNone(found, "推的那條應該要收到")
            self.assertEqual(found[0], "delivery")
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
