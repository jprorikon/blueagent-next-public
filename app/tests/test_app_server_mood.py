#!/usr/bin/env python3
"""S4 平台公开心情：人走 HTTP、Agent 走 MCP，可见域与私有 affect 隔离。"""

from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.config import AppConfig
from app.server.http import make_server


class AppServerMoodCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-app-mood-")
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        accounts = root / "accounts.json"
        rows = [
            ("owner", "owner", "主人", "owner-token", None),
            ("aster", "entity", "アスター", "aster-token", None),
            ("nori", "entity", "ノリ", "nori-token", None),
            ("guest", "guest", "客人", "guest-token", ["aster"]),
        ]
        accounts.write_text(json.dumps({"schema_version": 1, "accounts": [
            {"id": identity, "kind": kind, "display": display,
             "visible_entities": visible,
             "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
            for identity, kind, display, token, visible in rows
        ]}), encoding="utf-8")
        self.server = make_server(AppConfig("127.0.0.1", 0, root / "data", accounts))
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
            headers={"Authorization": f"Bearer {token}" if token else "",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else None

    def mcp(self, name, arguments=None, token="aster-token"):
        status, reply = self.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }, token=token)
        self.assertEqual(status, 200, reply)
        return reply["result"]

    @staticmethod
    def payload(result):
        return json.loads(result["content"][0]["text"])

    def mood_events(self, token="owner-token"):
        status, body = self.request("GET", "/v1/events?after=0&limit=200", token=token)
        self.assertEqual(status, 200, body)
        return [row for row in body["events"] if row["type"] == "mood.updated"]

    def test_human_set_is_idempotent_and_never_copies_private_affect(self):
        status, empty = self.request("GET", "/v1/me/mood")
        self.assertEqual((status, empty["mood"]), (200, None))
        body = {"key": "happy", "note": "  今日も   よろしく  ",
                "valence": 0.99, "arousal": 1.0, "social_need": 1.0}
        status, first = self.request("PUT", "/v1/me/mood", body)
        self.assertEqual(status, 200, first)
        self.assertFalse(first["idempotent_replay"])
        self.assertEqual(first["mood"]["account_id"], "owner")
        self.assertEqual(first["mood"]["note"], "今日も よろしく")
        self.assertNotIn("valence", repr(first))
        self.assertNotIn("social_need", repr(first))

        status, replay = self.request("PUT", "/v1/me/mood", body)
        self.assertEqual(status, 200, replay)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["mood"]["event_id"], first["mood"]["event_id"])
        self.assertEqual(replay["mood"]["updated_at"], first["mood"]["updated_at"])
        self.assertEqual(len(self.mood_events()), 1)

        status, denied = self.request("PUT", "/v1/me/mood", {"key": "happy"}, token="aster-token")
        self.assertEqual((status, denied["error"]), (403, "mood_use_mcp"))
        status, denied = self.request("GET", "/v1/moods", token="")
        self.assertEqual((status, denied["error"]), (401, "unauthorized"))

    def test_agent_mcp_identity_tools_and_public_event(self):
        listed = self.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token="aster-token")[1]
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertIn("mood.list", names)
        self.assertIn("mood.set", names)
        result = self.mcp("mood.set", {"key": "excited", "note": "新しい発見！",
                                       "account_id": "owner", "entity": "owner"})
        self.assertFalse(result.get("isError"), result)
        first = self.payload(result)
        self.assertEqual(first["mood"]["account_id"], "aster")
        self.assertEqual(first["mood"]["kind"], "entity")
        replay = self.payload(self.mcp("mood.set", {"key": "excited", "note": "新しい発見！"}))
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["mood"]["event_id"], first["mood"]["event_id"])

        events = self.mood_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], {"kind": "entity", "id": "aster"})
        self.assertEqual(events[0]["payload"]["mood"]["key"], "excited")
        self.assertNotIn("affect", repr(events))
        listed = self.payload(self.mcp("mood.list"))
        self.assertEqual([row["account_id"] for row in listed["moods"]], ["aster"])
        denied = self.mcp("mood.set", {"key": "happy"}, token="owner-token")
        self.assertEqual(denied["content"][0]["text"], "mood_entity_only")

    def test_guest_visible_domain_and_validation(self):
        self.assertFalse(self.mcp("mood.set", {"key": "happy"}, token="aster-token").get("isError"))
        self.assertFalse(self.mcp("mood.set", {"key": "down", "note": "内緒ではなく公開"},
                                  token="nori-token").get("isError"))
        status, visible = self.request("GET", "/v1/moods", token="guest-token")
        self.assertEqual(status, 200, visible)
        self.assertEqual([row["account_id"] for row in visible["moods"]], ["aster"])
        guest_events = self.mood_events(token="guest-token")
        self.assertEqual(len(guest_events), 1)
        self.assertEqual(guest_events[0]["actor"]["id"], "aster")
        self.assertNotIn("内緒ではなく公開", repr(guest_events))

        status, invalid = self.request("PUT", "/v1/me/mood", {"key": "secret"})
        self.assertEqual((status, invalid["error"]), (400, "invalid_mood"))
        status, long_note = self.request("PUT", "/v1/me/mood", {"key": "calm", "note": "x" * 81})
        self.assertEqual((status, long_note["error"]), (413, "mood_note_too_long"))
        invalid_agent = self.mcp("mood.set", {"key": "unknown"})
        self.assertEqual(invalid_agent["content"][0]["text"], "invalid_mood")


if __name__ == "__main__":
    unittest.main(verbosity=2)
