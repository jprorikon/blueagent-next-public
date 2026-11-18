#!/usr/bin/env python3
"""她打給主人，跨進程：撥號器 → App Server 響鈴 → 主人接聽（brain `answer`）→ 音訊幀 → 她的聲音；拒接 ⇒ 日誌。"""

from __future__ import annotations

import json
import time
import unittest
import urllib.error
import urllib.request

from demo_common import *  # noqa: F401,F403
import test_app_voice_e2e as base                    # 模組引入：不讓 loader 在這裡再跑一遍那邊的案例
from test_app_voice_e2e import frame

from agent.adapter.app_call_out import AppCallDialer
from agent.adapter.app_server import AppServerClient


class AppCallOutEndToEndCase(unittest.TestCase):
    setUp_base = base.AppVoiceEndToEndCase.setUp
    post, get = base.AppVoiceEndToEndCase.post, base.AppVoiceEndToEndCase.get

    def setUp(self):
        self.setUp_base()
        self.dialer = AppCallDialer(
            self.state.app, {"mira": AppServerClient(self.base, "mira-token", timeout=5)},
            log=lambda _m: None, poll_s=0.05, watch_s=10)
        self.state.app.voice.dialer = self.dialer

    def ring(self):
        reply = self.state.app.voice.ring("mira", principal="owner", reason="声が聞きたくなった")
        self.assertTrue(reply["ok"], reply)
        self.assertIsNone(self.state.app.voice.active("mira"))     # 主人接之前，她那頭沒有通話
        events = self.get("/v1/events")["events"]
        self.assertEqual([event["type"] for event in events], ["call.incoming"])
        self.assertEqual(events[0]["payload"]["call_id"], reply["call_id"])
        return reply["call_id"]

    def test_owner_accepts_and_hears_her(self):
        call_id = self.ring()
        accepted = self.post(f"/v1/calls/{call_id}/accept", {})
        self.addCleanup(self.post, f"/v1/calls/{call_id}/hangup", {})
        self.assertEqual(accepted["state"], "live")
        session = self.state.app.voice.active("mira")
        self.assertEqual((session.id, session.meta["pickup"]), (call_id, "outgoing"))
        self.assertIn("声が聞きたくなった", session.meta["pickup_note"])
        self.post(f"/v1/calls/{call_id}/frames", {"seq": 1, "audio": frame(300)})
        for seq in range(2, 10):
            self.post(f"/v1/calls/{call_id}/frames", {"seq": seq, "audio": frame(100, 0)})
        seen, cursor = [], 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "done" not in seen:
            page = self.get(f"/v1/calls/{call_id}/events?after={cursor}")
            seen += [event["name"] for event in page["events"]]
            cursor = page["next_cursor"]
            time.sleep(0.05)
        self.assertIn("speech", seen)
        self.dialer.join(call_id)
        self.assertFalse([row for row in self.state.app.activity_log.entries("mira", limit=10)
                          if row["kind"] == "call_unanswered"])

    def test_owner_declines_and_her_log_says_so(self):
        call_id = self.ring()
        self.assertEqual(self.post(f"/v1/calls/{call_id}/decline", {})["state"], "declined")
        self.dialer.join(call_id)
        rows = [row for row in self.state.app.activity_log.entries("mira", limit=10)
                if row["kind"] == "call_unanswered"]
        self.assertEqual(len(rows), 1)
        self.assertIn("出てもらえなかった", rows[0]["text"])
        self.assertIsNone(self.state.app.voice.active("mira"))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(f"/v1/calls/{call_id}/accept", {})
        self.assertEqual(caught.exception.code, 410)
        types = [event["type"] for event in self.get("/v1/events")["events"]]
        self.assertEqual(types, ["call.incoming", "call.updated"])

    def test_owner_calling_her_while_she_rings_is_busy(self):
        call_id = self.ring()
        request = urllib.request.Request(
            self.base + "/v1/calls", data=json.dumps({"entity": "mira"}).encode(), method="POST",
            headers={"Authorization": "Bearer owner-token", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 409)
        self.post(f"/v1/calls/{call_id}/decline", {})
        self.dialer.join(call_id)


if __name__ == "__main__":
    unittest.main()
