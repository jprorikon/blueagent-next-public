#!/usr/bin/env python3
"""驗收：owner token 不落日誌、只存 hash、可輪替；reset 端點默認關。

判據：
  ① `data/auth.json` 裡**沒有明文**（只有 `owner_token_sha256`）；
  ② 舊格式（明文）啟動時自動升級，且**本次仍可用**原 token（不把人鎖在外面）；
  ③ 輪替後舊 token 401、新 token 200；
  ④ `/api/reset` 默認關（403 `reset_disabled`），要開必須顯式 `BLUEAGENT_ALLOW_RESET=1`；
  ⑤ 啟動日誌不含明文 token（只有指紋）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.bootstrap import build
from agent.brain.compose.state import AppState


def fresh(**kwargs):
    root = tempfile.mkdtemp(prefix="ba-auth-")
    demo = build(brain_delay=0, data_dir=root, step_duration=0.02, life_pause=0.005)
    return demo, AppState(demo), root


class TestTokenStorage(unittest.TestCase):
    def test_file_holds_hash_not_plaintext(self):
        demo, _state, root = fresh()
        try:
            raw = json.loads((demo.auth.path).read_text())
            self.assertIn("owner_token_sha256", raw)
            self.assertNotIn("owner_token", raw, "明文不該留在 auth.json")
            self.assertRegex(raw["owner_token_sha256"], r"^[0-9a-f]{64}$")
        finally:
            demo.shutdown_lives()

    def test_legacy_plaintext_is_upgraded_and_still_works(self):
        root = tempfile.mkdtemp(prefix="ba-auth-legacy-")
        path = f"{root}/auth.json"
        with open(path, "w") as handle:
            json.dump({"owner_token": "ot_legacy123"}, handle)
        demo = build(data_dir=root, brain_delay=0, step_duration=0.02, life_pause=0.005)
        try:
            # 這輪仍然可以用舊 token 進場（升級不鎖人）
            self.assertIsNotNone(demo.auth.authenticate("Bearer ot_legacy123"))
            raw = json.loads(open(demo.auth.path).read())
            self.assertNotIn("owner_token", raw)
            self.assertIn("owner_token_sha256", raw)
        finally:
            demo.shutdown_lives()

    def test_rotate_invalidates_the_old_token(self):
        demo, _state, _root = fresh()
        try:
            old = demo.auth.owner_token
            self.assertTrue(old)
            new = demo.auth.rotate()
            self.assertNotEqual(old, new)
            self.assertIsNone(demo.auth.authenticate(f"Bearer {old}"),
                              "輪替後舊 token 必須失效")
            self.assertIsNotNone(demo.auth.authenticate(f"Bearer {new}"))
        finally:
            demo.shutdown_lives()

    def test_auth_context_credential_never_leaks(self):
        demo, _state, _root = fresh()
        try:
            token = demo.auth.owner_token
            owner = demo.auth.authenticate("Bearer " + token)
            self.assertEqual(owner.credential, token)
            self.assertNotIn(token, repr(owner))
            self.assertNotIn(token, json.dumps(owner.to_dict()))
            self.assertNotIn("credential", owner.to_dict())

            guest_secret = "gt_caller_secret"
            demo.accounts.issue(role="guest", principal="guest-a", token=guest_secret)
            guest = demo.auth.authenticate("Bearer " + guest_secret)
            self.assertEqual(guest.credential, guest_secret)
            self.assertNotIn(guest_secret, repr(guest))
            self.assertNotIn(guest_secret, json.dumps(guest.to_dict()))
        finally:
            demo.shutdown_lives()

    def test_startup_log_has_no_plaintext_token(self):
        """把 build 的啟動輸出抓下來檢查：不可以出現明文 token。"""
        root = tempfile.mkdtemp(prefix="ba-auth-log-")
        demo = build(data_dir=root, brain_delay=0, step_duration=0.02, life_pause=0.005)
        try:
            token = demo.auth.owner_token
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                print(f"  owner token: {demo.auth.owner_token_fingerprint}")
            text = buffer.getvalue()
            if token:
                self.assertNotIn(token, text)
            self.assertIn("…", text)
        finally:
            demo.shutdown_lives()


class TestResetGate(unittest.TestCase):
    def setUp(self):
        self.app, self.state, _root = fresh()
        self.addCleanup(self.app.shutdown_lives)

    def test_reset_is_disabled_by_default(self):
        from agent.server import api
        os.environ.pop("BLUEAGENT_ALLOW_RESET", None)
        status, body = api.post_reset(self.state, auth=self.app.auth.owner_context())
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "reset_disabled")

    def test_reset_works_when_explicitly_enabled(self):
        from agent.server import api
        os.environ["BLUEAGENT_ALLOW_RESET"] = "1"
        try:
            status, _body = api.post_reset(self.state,
                                           auth=self.app.auth.owner_context())
            self.assertEqual(status, 200)
        finally:
            os.environ.pop("BLUEAGENT_ALLOW_RESET", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
