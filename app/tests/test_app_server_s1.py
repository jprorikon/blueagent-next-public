#!/usr/bin/env python3
"""S1 独立 App Server：真实 HTTP／MCP／SSE、账号边界与断线重连。"""

from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.config import AppConfig
from app.server.http import make_server


class AppServerS1Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-app-s1-")
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        accounts = root / "accounts.json"
        rows = [
            ("owner", "owner", "主人", "owner-token"),
            ("aster", "entity", "アスター", "aster-token"),
            ("nori", "entity", "ノリ", "nori-token"),
        ]
        accounts.write_text(json.dumps({"schema_version": 1, "accounts": [
            {"id": identity, "kind": kind, "display": display,
             "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
            for identity, kind, display, token in rows
        ]}), encoding="utf-8")
        config = AppConfig("127.0.0.1", 0, root / "data", accounts)
        self.server = make_server(config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.thread.join, 2.0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, token="owner-token", headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token}" if token else "",
                     "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else None

    def mcp(self, name, arguments=None, token="aster-token", msg_id=1):
        status, reply = self.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }, token=token)
        self.assertEqual(status, 200, reply)
        return reply["result"]

    @staticmethod
    def payload(result):
        return json.loads(result["content"][0]["text"])

    def test_health_auth_and_account_shape(self):
        status, health = self.request("GET", "/health", token="")
        self.assertEqual((status, health["ok"]), (200, True))
        status, denied = self.request("GET", "/v1/me", token="wrong")
        self.assertEqual((status, denied["error"]), (401, "unauthorized"))
        status, me = self.request("GET", "/v1/me", token="aster-token")
        self.assertEqual(status, 200)
        self.assertEqual(me["account"]["id"], "aster")
        self.assertNotIn("token", repr(me))
        self.assertNotIn("aster-token", self.server.services.accounts.path.read_text())

    def test_remote_mcp_identity_permissions_and_event_cursor(self):
        initialized = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1,
                                                    "method": "initialize"}, token="aster-token")[1]
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "app-server")
        listed = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 2,
                                               "method": "tools/list"}, token="aster-token")[1]
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]],
                         ["feed", "get", "post", "reply", "like", "delete",
                          "chat.conversation.open", "chat.agent_room.open", "chat.conversations",
                          "chat.messages", "chat.message.send", "media.get", "sticker.list",
                          "mood.list", "mood.set", "location.get"])

        created = self.mcp("post", {"text": "独立服务投稿", "author_id": "owner",
                                    "entity": "owner", "actor": "owner"})
        self.assertFalse(created.get("isError"), created)
        post = self.payload(created)["post"]
        self.assertEqual((post["author_id"], post["author_kind"]), ("aster", "entity"))
        denied = self.mcp("delete", {"id": post["id"]}, token="nori-token")
        self.assertTrue(denied.get("isError"))
        self.assertIn("denied", denied["content"][0]["text"])

        status, events = self.request("GET", "/v1/events?after=0&limit=1", token="nori-token")
        self.assertEqual(status, 200)
        self.assertEqual(events["events"][0]["actor"], {"kind": "entity", "id": "aster"})
        self.assertEqual(events["events"][0]["payload"]["post"]["body"], "独立服务投稿")
        status, empty = self.request("GET", f"/v1/events?after={events['next_cursor']}")
        self.assertEqual((status, empty["events"]), (200, []))
        status, bad = self.request("GET", "/v1/events?after=bad")
        self.assertEqual((status, bad["error"]), (400, "bad_cursor"))
        status, expired = self.request("GET", "/v1/events", headers={"Last-Event-ID": "evt_missing"})
        self.assertEqual((status, expired["error"], expired["resync_required"]),
                         (409, "cursor_expired", True))

    def test_rejects_unverified_media_and_contains_unexpected_mcp_errors(self):
        rejected = self.mcp("post", {"text": "伪造媒体", "media": ["missing"]})
        self.assertEqual(rejected["content"][0]["text"], "unknown_media")
        self.assertTrue(rejected["isError"])

        def fail_feed(*_args, **_kwargs):
            raise RuntimeError("secret database path")

        self.server.services.board.feed = fail_feed
        failed = self.mcp("feed")
        self.assertEqual(failed["content"][0]["text"], "internal_error")
        self.assertNotIn("secret", repr(failed))
        status, health = self.request("GET", "/health", token="")
        self.assertEqual(status, 200)

    def test_sse_rejects_bad_cursor_before_starting_stream(self):
        status, body = self.request("GET", "/v1/events?after=bad",
                                    headers={"Accept": "text/event-stream"})
        self.assertEqual((status, body["error"]), (400, "bad_cursor"))

    def test_sse_closes_when_the_token_stops_being_valid(self):
        """多設備配對（2026-11-16）：端末被撤銷後，已經開著的常駐 SSE 也要斷。"""
        from unittest import mock
        from app.server import http as app_http
        accounts = self.server.services.accounts
        real = accounts.authenticate
        revoked = threading.Event()

        def authenticate(header):
            return None if revoked.is_set() else real(header)

        request = urllib.request.Request(
            self.base + "/v1/events",
            headers={"Authorization": "Bearer owner-token", "Accept": "text/event-stream"})
        with mock.patch.object(app_http, "SSE_REAUTH_S", 0.1), \
                mock.patch.object(accounts, "authenticate", side_effect=authenticate):
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertIn(b"event: ready", response.readline() + response.readline()
                              + response.readline() + response.readline())
                time.sleep(0.3)
                self.assertFalse(revoked.is_set())
                revoked.set()
                started = time.monotonic()
                try:
                    while response.readline():      # 收線＝讀到 EOF
                        self.assertLess(time.monotonic() - started, 4.0, "撤銷後沒有收線")
                except TimeoutError:
                    self.fail("撤銷後沒有收線（讀到逾時）")
        self.assertLess(time.monotonic() - started, 4.0)

    def test_sse_reconnects_after_last_event_id(self):
        first = self.payload(self.mcp("post", {"text": "第一条"}))["post"]
        second = self.payload(self.mcp("post", {"text": "第二条"}))["post"]
        _, events = self.request("GET", "/v1/events")
        first_event, second_event = events["events"]
        self.assertEqual(first_event["entity"]["id"], first["id"])
        self.assertEqual(second_event["entity"]["id"], second["id"])

        request = urllib.request.Request(
            self.base + "/v1/events",
            headers={"Authorization": "Bearer aster-token", "Accept": "text/event-stream",
                     "Last-Event-ID": first_event["event_id"]})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            frame = {}
            while True:
                line = response.readline().decode().strip()
                if not line:
                    if "data" in frame:
                        if frame.get("event") == "ready":
                            self.assertNotIn("id", frame)
                            frame = {}
                            continue
                        break
                    frame = {}
                    continue
                key, _, value = line.partition(": ")
                frame[key] = value
        self.assertEqual(frame["id"], second_event["event_id"])
        self.assertEqual(frame["event"], "post.created")
        self.assertEqual(json.loads(frame["data"])["entity"]["id"], second["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
