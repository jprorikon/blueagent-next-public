#!/usr/bin/env python3
"""Stance：角色→人的慢变量，按 principal 隔离，只管语气不管记忆。"""

from __future__ import annotations

import json
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403


class TestStance(unittest.TestCase):
    def test_keyed_by_principal_not_device(self):
        state = make_state()
        try:
            service = state.app.stances
            self.assertEqual(service.get("mira", "owner").weight("lover"), 55)
            service.set("mira", "alice", weights={"master": 15})
            self.assertEqual(service.get("mira", "alice").weight("master"), 15)
            self.assertEqual(service.get("mira", "owner").weight("master"), 70)
            owner_block = service.render("mira", "owner",
                                         names={"owner": "主人"})
            alice_block = service.render("mira", "alice",
                                         names={"alice": "Alice"})
            self.assertIn("主人", owner_block)
            self.assertIn("友だち", alice_block)
            self.assertNotIn("恋人", alice_block)
        finally:
            state.shutdown()

    def test_top_k_budget_and_no_raw_numbers(self):
        state = make_state()
        try:
            service = state.app.stances
            service.set("mira", "owner",
                        weights={"friend": 80, "master": 30, "lover": 20})
            block = service.render("mira", "owner", max_chars=120)
            body = block.removeprefix("[[stance]]\n").removesuffix("\n[[/stance]]")
            self.assertLessEqual(len(body), 120)
            self.assertIn("友だち", body)
            self.assertIn("オーナー（オーナー）", body)
            self.assertNotIn("恋人", body)
            self.assertNotIn("80", body)
        finally:
            state.shutdown()

    def test_prompt_uses_speaker_stance_and_skips_ownerless(self):
        state = make_state()
        captured = {}

        def fake_turn(request):
            captured["system"] = request.system
            yield ("done", {"text": "好"})

        state.app.brain.run_turn = fake_turn
        try:
            state.send("你好", entity="mira", principal="alice")
            self.assertIn("[[stance]]", captured["system"])
            self.assertIn("友だち", captured["system"])
            self.assertNotIn("恋人", captured["system"])

            state.send("你好", entity="mira", principal="owner")
            self.assertIn("主「オーナー（オーナー）」", captured["system"])

            state.send("我在桌边记录", entity="mira", ownerless=True)
            self.assertNotIn("\n[[stance]]\n", captured["system"])
        finally:
            state.shutdown()

    def test_group_renders_speaker_and_addressed(self):
        state = make_state()
        try:
            block = state.app.stances.render(
                "mira", "alice", addressed="bob", max_chars=120,
                names={"alice": "Alice", "bob": "Bob"})
            self.assertIn("Alice に対して", block)
            self.assertIn("Bob に対して", block)
            body = block.removeprefix("[[stance]]\n").removesuffix("\n[[/stance]]")
            self.assertLessEqual(len(body), 120)
        finally:
            state.shutdown()

    def test_persists_and_does_not_overwrite_on_rebuild(self):
        root = tempfile.mkdtemp(prefix="ba-stance-")
        demo = build(brain_delay=0, data_dir=root, step_duration=0.02,
                     life_pause=0.005)
        state = AppState(demo)
        state.set_stance(entity="mira", principal="alice",
                         weights={"lover": 88})
        state.shutdown()

        again = build(brain_delay=0, data_dir=root, step_duration=0.02,
                      life_pause=0.005)
        try:
            self.assertEqual(again.stances.get("mira", "alice").weight("lover"), 88)
            self.assertTrue(again.stance_store.path("mira").is_file())
        finally:
            again.shutdown_lives()

    def test_guest_approval_seeds_stance_for_each_entity(self):
        state = make_state()
        try:
            joined = state.join_guest(invite_code="blueagent", discord_id="8080",
                                      display_name="阿森", declared_kind="friend")
            approved = state.approve_guest(
                joined["request_id"], final_kind="family", final_label="家人")
            principal = approved["request"]["principal_id"]
            # 2026-10-21：lumi 下線、nori 上線（角色集＝mira/aster/nori）
            for entity, expected in (("mira", 45), ("aster", 40), ("nori", 40)):
                stance = state.app.stances.get(entity, principal)
                self.assertIsNotNone(stance)
                self.assertEqual(stance.weight("family"), expected)
        finally:
            state.shutdown()

    def test_high_stance_does_not_open_pair_memory(self):
        state = make_state()
        try:
            a = state.join_guest(invite_code="blueagent", discord_id="8181",
                                 display_name="A", declared_kind="friend")
            a_pid = state.approve_guest(a["request_id"], final_kind="friend")[
                "request"]["principal_id"]
            b = state.join_guest(invite_code="blueagent", discord_id="8282",
                                 display_name="B", declared_kind="friend")
            b_pid = state.approve_guest(b["request_id"], final_kind="friend")[
                "request"]["principal_id"]
            state.send("记住：我和 B 在准备一个惊喜", entity="mira",
                       principal=a_pid, group=True, participants=[a_pid, b_pid])
            state.set_stance(entity="mira", principal=a_pid,
                             weights={"friend": 100})
            owner_view = state.app.memory.snapshot({"owner"})["facts"]
            self.assertFalse(any(fact["visible"] and fact["layer"] == "pair"
                                 and "B 在准备" in fact["text"]
                                 for fact in owner_view))
        finally:
            state.shutdown()


class TestStanceHttp(unittest.TestCase):
    def test_owner_get_and_set(self):
        from agent.server import http as app_server

        state = make_state()
        app_server.set_state(state)
        owner = owner_headers(state)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(
                        base + "/api/owner/stances?entity=mira", headers=owner),
                    timeout=3) as response:
                listing = json.loads(response.read())
            self.assertTrue(listing["ok"])
            self.assertTrue(listing["stances"])

            body = json.dumps({"entity": "mira", "principal": "alice",
                               "weights": {"lover": 77}}).encode("utf-8")
            request = urllib.request.Request(
                base + "/api/owner/stances", data=body, method="POST",
                headers={**owner, "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=3) as response:
                changed = json.loads(response.read())
            self.assertTrue(changed["ok"])
            axes = {row["id"]: row["weight"] for row in changed["stance"]["axes"]}
            self.assertEqual(axes["lover"], 77)
        finally:
            httpd.shutdown()
            httpd.server_close()
            state.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
