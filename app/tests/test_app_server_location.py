#!/usr/bin/env python3
"""C-14：位置默认关闭、粗化、按角色授权、限时与撤销的隐私边界。"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.config import AppConfig
from app.server.http import make_server


class AppServerLocationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-app-location-")
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

    def enable(self, token="owner-token"):
        status, body = self.request("PUT", "/v1/me/location", {"enabled": True}, token=token)
        self.assertEqual(status, 200, body)
        return body

    def report(self, *, token="owner-token", device="device-owner-1",
               lat=35.6789, lon=139.7654, accuracy=12.0, source="ios"):
        return self.request("POST", "/v1/me/location", {
            "device": device, "lat": lat, "lon": lon,
            "accuracy": accuracy, "source": source,
        }, token=token)

    def events(self, event_type, token="owner-token"):
        status, body = self.request("GET", "/v1/events?after=0&limit=200", token=token)
        self.assertEqual(status, 200, body)
        return [row for row in body["events"] if row["type"] == event_type]

    def test_default_off_anonymous_and_consent_validation(self):
        status, body = self.request("GET", "/v1/me/location")
        self.assertEqual(status, 200, body)
        sharing = body["sharing"]
        self.assertFalse(sharing["enabled"])
        self.assertEqual(sharing["authorized_account_ids"], [])
        self.assertEqual((sharing["precision_m"], sharing["ttl_s"]), (1000, 1800))
        self.assertNotIn("lat", repr(body))

        status, denied = self.report()
        self.assertEqual((status, denied["error"]), (403, "location_consent_required"))
        status, denied = self.request("GET", "/v1/me/location", token="")
        self.assertEqual((status, denied["error"]), (401, "unauthorized"))
        status, denied = self.request("PUT", "/v1/me/location", {"enabled": "yes"})
        self.assertEqual((status, denied["error"]), (400, "invalid_location_consent"))
        status, denied = self.request("PUT", "/v1/me/location", {"enabled": True},
                                      token="aster-token")
        self.assertEqual((status, denied["error"]), (403, "location_human_only"))
        status, denied = self.request("POST", "/v1/me/location", {}, token="aster-token")
        self.assertEqual((status, denied["error"]), (403, "location_human_only"))

    def test_invalid_coordinates_device_and_source_are_rejected(self):
        self.enable()
        for lat, lon in [(None, 139.0), ({}, 139.0), ("bad", 139.0),
                         (math.nan, 139.0), (math.inf, 139.0),
                         (90.01, 139.0), (35.0, 180.01), (35.0, -math.inf)]:
            with self.subTest(lat=lat, lon=lon):
                status, body = self.report(lat=lat, lon=lon)
                self.assertEqual((status, body["error"]), (400, "bad_coordinates"))
        status, body = self.report(device="short")
        self.assertEqual((status, body["error"]), (400, "invalid_location_device"))
        status, body = self.report(device="bad device identifier")
        self.assertEqual((status, body["error"]), (400, "invalid_location_device"))
        status, body = self.report(source="gps")
        self.assertEqual((status, body["error"]), (400, "invalid_location_source"))

    def test_coarse_samples_private_events_and_agent_principal_filter(self):
        enabled = self.enable()
        self.assertEqual(enabled["sharing"]["authorized_account_ids"], ["aster", "nori"])
        self.assertEqual(enabled["sharing"]["purpose"], "weather-v1")
        status, first = self.report()
        self.assertEqual(status, 200, first)
        location = first["location"]
        self.assertEqual((location["lat"], location["lon"]), (35.68, 139.77))
        self.assertEqual(location["accuracy"], 1000)
        self.assertEqual(location["precision"], "coarse")
        self.assertTrue(first["changed"])
        self.assertNotIn("35.6789", repr(first))
        self.assertNotIn("139.7654", repr(first))

        with sqlite3.connect(self.server.services.location.db_path) as db:
            stored = db.execute("SELECT lat,lon,accuracy_m FROM location_sample").fetchone()
            social_text = " ".join(str(row) for row in db.execute(
                "SELECT body FROM post").fetchall())
            message_text = " ".join(str(row) for row in db.execute(
                "SELECT text FROM message_part").fetchall())
            location_events = " ".join(str(row) for row in db.execute(
                "SELECT payload FROM event WHERE type LIKE 'location.%'").fetchall())
        self.assertEqual(stored, (35.68, 139.77, 1000.0))
        self.assertNotIn("35.6789", social_text + message_text + location_events)
        self.assertNotIn("139.7654", social_text + message_text + location_events)
        self.assertNotIn("35.68", location_events)
        self.assertNotIn("139.77", location_events)
        self.assertNotIn('"lat"', location_events)
        self.assertNotIn('"lon"', location_events)

        self.assertEqual(len(self.events("location.updated")), 1)
        self.assertEqual(len(self.events("location.updated", token="aster-token")), 1)
        self.assertEqual(len(self.events("location.updated", token="nori-token")), 1)
        self.assertEqual(self.events("location.updated", token="guest-token"), [])
        status, repeated = self.report()
        self.assertEqual(status, 200, repeated)
        self.assertFalse(repeated["changed"])
        self.assertEqual(len(self.events("location.updated", token="aster-token")), 1)

        status, moved = self.report(lat=35.69001)
        self.assertEqual(status, 200, moved)
        self.assertTrue(moved["changed"])
        self.assertEqual(len(self.events("location.updated", token="aster-token")), 2)
        latest = self.payload(self.mcp("location.get", {"principal": "owner"}))["location"]
        self.assertEqual(latest["lat"], 35.69)
        self.assertEqual(latest["account_id"], "owner")
        self.assertIsNone(self.payload(self.mcp("location.get", {"principal": "guest"}))["location"])
        for token in ("owner-token", "guest-token"):
            denied = self.mcp("location.get", token=token)
            self.assertTrue(denied.get("isError"))
            self.assertEqual(denied["content"][0]["text"], "location_entity_only")

    def test_accounts_devices_and_visibility_are_isolated(self):
        self.enable()
        self.enable(token="guest-token")
        status, owner = self.report(device="shared-device-1", lat=35.6789)
        self.assertEqual(status, 200, owner)
        status, guest = self.report(token="guest-token", device="shared-device-1",
                                    lat=34.6789, lon=135.4321, source="android")
        self.assertEqual(status, 200, guest)
        status, other = self.report(device="device-owner-2", lat=36.1234)
        self.assertEqual(status, 200, other)

        owner_status = self.request("GET", "/v1/me/location")[1]["sharing"]
        guest_status = self.request("GET", "/v1/me/location", token="guest-token")[1]["sharing"]
        self.assertEqual({row["device"] for row in owner_status["devices"]},
                         {"shared-device-1", "device-owner-2"})
        self.assertEqual([row["device"] for row in guest_status["devices"]], ["shared-device-1"])
        self.assertNotIn("lat", repr(owner_status) + repr(guest_status))

        aster_guest = self.payload(self.mcp("location.get", {"principal": "guest"}))["location"]
        self.assertEqual((aster_guest["account_id"], aster_guest["lat"]), ("guest", 34.68))
        self.assertTrue(all(row["payload"]["location"]["account_id"] == "guest"
                            for row in self.events("location.updated", token="guest-token")))

    def test_expiry_and_revoke_delete_samples_without_public_leak(self):
        self.enable()
        self.enable(token="guest-token")
        self.report()
        self.report(token="guest-token", device="device-guest-1", lat=34.5678)
        with sqlite3.connect(self.server.services.location.db_path) as db:
            db.execute("UPDATE location_sample SET expires_at=1 WHERE account_id='owner'")
        self.assertIsNone(self.payload(self.mcp("location.get", {"principal": "owner"}))["location"])
        self.assertEqual(self.request("GET", "/v1/me/location")[1]["sharing"]["devices"], [])
        surviving = self.payload(self.mcp("location.get", {"principal": "guest"}))["location"]
        self.assertEqual(surviving["account_id"], "guest")

        status, revoked = self.request("PUT", "/v1/me/location", {"enabled": False})
        self.assertEqual(status, 200, revoked)
        self.assertFalse(revoked["sharing"]["enabled"])
        with sqlite3.connect(self.server.services.location.db_path) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM location_sample WHERE account_id='owner'").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM location_sample WHERE account_id='guest'").fetchone()[0], 1)
        for token in ("owner-token", "aster-token", "nori-token"):
            rows = self.events("location.revoked", token=token)
            self.assertEqual(len(rows), 1, token)
            self.assertNotIn("lat", repr(rows))
            self.assertNotIn("lon", repr(rows))
            self.assertNotIn("location", rows[0]["payload"])
        self.assertEqual(self.events("location.revoked", token="guest-token"), [])
        self.assertIsNone(self.payload(self.mcp("location.get", {"principal": "owner"}))["location"])
        self.assertEqual(self.payload(self.mcp("location.get", {"principal": "guest"}))["location"]["account_id"],
                         "guest")
        status, denied = self.report()
        self.assertEqual((status, denied["error"]), (403, "location_consent_required"))

        # 重新开启也不会恢复撤销前的样本；公共投稿和匿名事件均不含位置。
        self.enable()
        self.assertIsNone(self.payload(self.mcp("location.get", {"principal": "owner"}))["location"])
        status, feed = self.request("GET", "/v1/feed", token="aster-token")
        self.assertEqual(status, 200, feed)
        self.assertNotIn("35.68", repr(feed))
        status, denied = self.request("GET", "/v1/events?after=0", token="")
        self.assertEqual((status, denied["error"]), (401, "unauthorized"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
