#!/usr/bin/env python3
"""Guest：关系声明、主人拍板、principal/关系图/白名单、持久化。"""

from __future__ import annotations

import json
import threading
import urllib.request

from demo_common import *  # noqa: F401,F403
from http.server import ThreadingHTTPServer


class TestGuestFlow(unittest.TestCase):
    def test_declared_relation_then_owner_decides_final(self):
        state = make_state()
        joined = state.join_guest(
            invite_code="blueagent", discord_id="123456", display_name="小林",
            declared_kind="classmate", declared_label="大学同学",
            declared_note="一起上过课，很久没见")
        self.assertTrue(joined["ok"])
        self.assertEqual(joined["declared"]["kind"], "classmate")
        pending = state.owner_guests()["guests"]["pending"]
        self.assertEqual(pending[0]["declared"]["label"], "大学同学")

        approved = state.approve_guest(
            joined["request_id"], final_kind="friend", final_label="朋友", trust=0.7)
        self.assertTrue(approved["ok"])
        principal_id = approved["request"]["principal_id"]
        self.assertIsNotNone(state.app.principals.get(principal_id))
        relation = state.app.social.relation("owner", principal_id)
        self.assertEqual(relation.kind, "friend")          # 主人拍板覆盖声明
        self.assertEqual(relation.label, "朋友")
        self.assertEqual(state.app.guest_store.whitelist()[0]["discord_id"], "123456")
        status = state.guest_status(joined["request_secret"])
        self.assertEqual(status["request"]["status"], "approved")
        self.assertTrue(status["request"]["token"].startswith("gt_"))

    def test_reject_does_not_create_guest(self):
        state = make_state()
        joined = state.join_guest(invite_code="blueagent", discord_id="999",
                                  display_name="路人", declared_kind="other")
        state.reject_guest(joined["request_id"])
        self.assertEqual(state.guest_status(joined["request_secret"])["request"]["status"],
                         "rejected")
        self.assertEqual(state.app.guest_store.whitelist(), [])

    def test_new_guest_declares_relation_to_existing_guest_and_owner_decides(self):
        state = make_state()
        a = state.join_guest(invite_code="blueagent", discord_id="111",
                             display_name="A", declared_kind="friend",
                             declared_label="朋友")
        a_pid = state.approve_guest(a["request_id"], final_kind="friend",
                                    final_label="朋友")["request"]["principal_id"]
        b = state.join_guest(
            invite_code="blueagent", discord_id="222", display_name="B",
            declared_kind="friend", declared_label="朋友",
            guest_relations=[{"other_discord_id": "111", "kind": "classmate",
                              "label": "同学", "note": "一起上过课"}])
        pending = state.owner_guests()["guests"]["pending"]
        self.assertEqual(pending[0]["declared_guests"][0]["other_principal"], a_pid)
        b_approved = state.approve_guest(
            b["request_id"], final_kind="friend", final_label="朋友",
            final_guest_relations=[{"other_principal": a_pid,
                                    "kind": "acquaintance", "label": "只是认识"}])
        b_pid = b_approved["request"]["principal_id"]
        relation = state.app.social.relation(a_pid, b_pid)
        self.assertEqual(relation.kind, "acquaintance")
        self.assertEqual(relation.label, "只是认识")
        a_request = state.app.guest_store.get(a["request_id"])
        self.assertTrue(any(item.other_principal == b_pid
                            for item in a_request.final_guests))

    def test_pair_memory_does_not_leak_to_master(self):
        state = make_state()
        a = state.join_guest(invite_code="blueagent", discord_id="333",
                             display_name="A", declared_kind="friend")
        a_pid = state.approve_guest(a["request_id"], final_kind="friend")["request"]["principal_id"]
        b = state.join_guest(invite_code="blueagent", discord_id="444",
                             display_name="B", declared_kind="friend")
        b_pid = state.approve_guest(b["request_id"], final_kind="friend")["request"]["principal_id"]
        # ⚠️ 2026-10-22 owner 定：旁聽（OBSERVE）不寫記憶 ⇒ 要造兩人記憶，這句話必須
        # **叫到她**（addressed_to="entity"），否則她只是旁聽、不會沉成事實。
        state.send("记住：我和 B 在准备一个惊喜", entity="mira",
                   principal=a_pid, group=True, participants=[a_pid, b_pid],
                   mention=True, addressed_to="entity")
        memory = state.app.memory
        room_facts = [fact for fact in memory.snapshot({a_pid, b_pid}, entity="mira")["facts"]
                      if "B 在准备" in fact["text"]]
        self.assertTrue(room_facts)
        # 她 + A + B ＝ 三人房 ⇒ 層是 circle（2026-10-23：層數含她在內算）
        self.assertEqual(room_facts[0]["layer"], "circle")
        # 主人不在那間房 ⇒ 說不出來
        owner_view = memory.snapshot({"owner"}, entity="mira")["facts"]
        self.assertFalse(any(fact["visible"] and "B 在准备" in fact["text"]
                             for fact in owner_view))
        # A 本來就在那間房 ⇒ 子集即可，能說（2026-10-23 owner：只看子集）
        a_view = memory.snapshot({a_pid}, entity="mira")["facts"]
        self.assertTrue(any(fact["visible"] and "B 在准备" in fact["text"]
                            for fact in a_view))
        # 兩個當事人一起在場 ⇒ 也可讀
        together = memory.snapshot({a_pid, b_pid}, entity="mira")["facts"]
        self.assertTrue(any(fact["visible"] and "B 在准备" in fact["text"]
                            for fact in together))

    def test_guest_survives_rebuild(self):
        root = tempfile.mkdtemp(prefix="ba-guest-")
        demo = build(brain_delay=0, data_dir=root, step_duration=0.02, life_pause=0.005)
        demo.principals.register(Principal("owner", "主人", owner=True), replace=True)
        state = AppState(demo)
        joined = state.join_guest(invite_code="blueagent", discord_id="555",
                                  display_name="阿远", declared_kind="friend")
        state.approve_guest(joined["request_id"], final_kind="friend",
                            final_label="老友", trust=0.8)
        demo.shutdown_lives()

        again = build(brain_delay=0, data_dir=root, step_duration=0.02, life_pause=0.005)
        again.principals.register(Principal("owner", "主人", owner=True), replace=True)
        self.assertIsNotNone(again.principals.get("guest_555"))
        relation = again.social.relation("owner", "guest_555")
        self.assertEqual(relation.label, "老友")
        again.shutdown_lives()


class TestGuestHttp(unittest.TestCase):
    def test_join_approve_whitelist(self):
        from agent.server import http as app_server

        state = make_state()
        app_server.set_state(state)
        owner = owner_headers(state)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            request = urllib.request.Request(
                base + "/api/guests/join",
                data=json.dumps({"invite_code": "blueagent", "discord_id": "777",
                                 "display_name": "小雨",
                                 "declared": {"kind": "colleague", "label": "同事",
                                              "note": "同组实习"}}).encode("utf-8"),
                method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=3) as response:
                joined = json.loads(response.read())
            approve = urllib.request.Request(
                base + f"/api/owner/guests/{joined['request_id']}/approve",
                data=json.dumps({"final_kind": "friend", "final_label": "朋友",
                                 "trust": 0.6}).encode("utf-8"),
                method="POST", headers={**owner, "Content-Type": "application/json"})
            with urllib.request.urlopen(approve, timeout=3) as response:
                approved = json.loads(response.read())
            self.assertTrue(approved["ok"])
            with urllib.request.urlopen(
                    urllib.request.Request(base + "/api/discord/whitelist",
                                           headers=owner), timeout=3) as response:
                whitelist = json.loads(response.read())
            self.assertEqual(whitelist["whitelist"][0]["discord_id"], "777")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
