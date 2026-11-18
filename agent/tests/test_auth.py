#!/usr/bin/env python3
"""鉴权：owner/guest token、guest 不能越权、guest 不能自报 principal。"""

from __future__ import annotations

import json
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403


class TestAuthHttp(unittest.TestCase):
    def setUp(self):
        from agent.server import http as app_server

        self.app_server = app_server
        self.state = make_state()
        app_server.set_state(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.state.shutdown()

    def call(self, path: str, *, method: str = "GET", body=None, token: str = ""):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + path, data=data,
                                         method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else {}

    def guest(self) -> tuple[str, str]:
        joined = self.state.join_guest(
            invite_code="blueagent", discord_id="9090", display_name="小岚",
            declared_kind="friend")
        approved = self.state.approve_guest(
            joined["request_id"], final_kind="friend", final_label="朋友")
        request = self.state.app.guest_store.get(joined["request_id"])
        return approved["request"]["principal_id"], request.token

    def test_state_and_send_require_token(self):
        status, _ = self.call("/api/state")
        self.assertEqual(status, 401)
        status, _ = self.call("/api/send", method="POST",
                              body={"text": "你好", "principal": "owner"})
        self.assertEqual(status, 401)

        owner = self.state.app.auth.owner_token
        status, payload = self.call("/api/state", token=owner)
        self.assertEqual(status, 200)
        status, payload = self.call("/api/send", method="POST",
                                    token=owner,
                                    body={"text": "你好", "principal": "owner"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])

    def test_guest_cannot_self_report_principal_or_group(self):
        pid, token = self.guest()
        status, payload = self.call(
            "/api/send", method="POST", token=token,
            body={"text": "你好", "principal": "owner", "group": True,
                  "participants": ["owner"], "ownerless": True})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload.get("error"))
        self.assertEqual(payload["state"]["view"]["principal"], pid)
        self.assertEqual(payload["room"], [pid])
        status, view = self.call("/api/state?principal=owner&room=owner", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(view["view"]["principal"], pid)
        self.assertNotIn("guests", view)

    def test_guest_cannot_call_owner_endpoints(self):
        _, token = self.guest()
        for path in ("/api/owner/guests", "/api/discord/whitelist",
                     "/api/owner/stances?entity=mira"):
            status, _ = self.call(path, token=token)
            self.assertEqual(status, 403, path)
        status, _ = self.call("/api/owner/stances", method="POST", token=token,
                              body={"entity": "mira", "principal": "owner"})
        self.assertEqual(status, 403)
        status, _ = self.call("/api/activity/start", method="POST", token=token,
                              body={"entity": "mira"})
        self.assertEqual(status, 403)

    def test_guest_snapshot_has_no_admin_blocks(self):
        _, token = self.guest()
        status, payload = self.call("/api/state", token=token)
        self.assertEqual(status, 200)
        self.assertNotIn("guests", payload)
        self.assertEqual(payload.get("pending"), [])
        self.assertEqual(payload.get("stances"), [])
        self.assertNotIn("episodes", payload)

    def test_revoked_guest_token_is_rejected(self):
        _, token = self.guest()
        request = self.state.app.guest_store.by_token(token)
        self.state.revoke_guest(request.id)
        status, _ = self.call("/api/send", method="POST", token=token,
                              body={"text": "还在吗"})
        self.assertEqual(status, 401)

    def test_guest_directory_needs_invite_or_token(self):
        status, _ = self.call("/api/guests/directory")
        self.assertEqual(status, 403)
        status, payload = self.call(
            "/api/guests/directory?invite_code=blueagent")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])


class TestAuthService(unittest.TestCase):
    def test_owner_token_survives_rebuild(self):
        root = tempfile.mkdtemp(prefix="ba-auth-")
        first = build(brain_delay=0, data_dir=root, step_duration=0.02,
                      life_pause=0.005)
        token = first.auth.owner_token
        self.assertTrue(token.startswith("ot_"))
        self.assertTrue((first.stance_store.root.parent / "auth.json").is_file())
        first.shutdown_lives()

        second = build(brain_delay=0, data_dir=root, step_duration=0.02,
                       life_pause=0.005)
        try:
            # 2026-10-22 起只存 hash：重建後**拿不到明文**，但同一個 token 仍然有效
            self.assertEqual(second.auth.owner_token, "",
                             "重建後不該再拿到明文 token")
            self.assertIsNotNone(second.auth.authenticate(f"Bearer {token}"),
                                 "同一個 token 重建後仍要能進場")
        finally:
            second.shutdown_lives()

    def test_rate_limit_bucket(self):
        state = make_state()
        try:
            auth = state.app.auth
            self.assertTrue(auth.allow("unit", limit=2, window_s=60))
            self.assertTrue(auth.allow("unit", limit=2, window_s=60))
            self.assertFalse(auth.allow("unit", limit=2, window_s=60))
        finally:
            state.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
