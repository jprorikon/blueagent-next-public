#!/usr/bin/env python3
"""帳號層：主人（master）與 guest 同一套物件，Unity 的兩種端靠它認身分。

判據：
  ① 簽發／驗證／輪替／撤銷——撤銷後舊 token 立刻 401，輪替後舊的也失效；
  ② **只存雜湊**：檔案裡找不到明文 token；
  ③ 主人啟動時自動收編成 `acct_owner`（master），不會把人鎖在外面；
  ④ `/api/me` 回「我是誰、哪種端、能做什麼、能跟哪些角色說話」，不含 token；
  ⑤ ACL：guest 看不到帳號列表、也不能簽發帳號。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.auth import Role
from agent.brain.runtime.identity.accounts import (AccountStore, hash_token, public_row)
from agent.brain.runtime.identity.auth import AuthService
from agent.brain.runtime.identity.guest_store import GuestStore
from agent.brain.domain.principal import PrincipalRegistry


class TestAccountStore(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.store = AccountStore(self.root / "accounts.json")

    def test_issue_and_verify(self):
        row, token = self.store.issue(role="guest", principal="alice")
        self.assertTrue(token.startswith("ba_"))
        found = self.store.verify(token)
        self.assertEqual(found["account_id"], row["account_id"])
        self.assertEqual(found["principal"], "alice")
        self.assertGreater(found["last_seen_at"], 0.0, "驗證會更新 last_seen")

    def test_only_hash_is_stored(self):
        _, token = self.store.issue(role="guest", principal="bob")
        raw = (self.root / "accounts.json").read_text(encoding="utf-8")
        self.assertNotIn(token, raw, "明文 token 不可以落檔")
        self.assertIn(hash_token(token), raw)

    def test_rotate_invalidates_old_token(self):
        row, old = self.store.issue(role="guest", principal="carol")
        result = self.store.rotate(row["account_id"])
        self.assertIsNotNone(result)
        _, new = result
        self.assertIsNone(self.store.verify(old), "舊 token 要立刻失效")
        self.assertIsNotNone(self.store.verify(new))

    def test_revoke_blocks_token(self):
        row, token = self.store.issue(role="guest", principal="dave")
        self.assertTrue(self.store.revoke(row["account_id"]))
        self.assertIsNone(self.store.verify(token))

    def test_list_and_snapshot(self):
        self.store.issue(role="master", principal="owner", account_id="acct_owner")
        self.store.issue(role="guest", principal="eve")
        rows = self.store.list()
        self.assertEqual(rows[0]["role"], "master", "master 排最前面")
        self.assertEqual(self.store.snapshot()["guest"], 1)

    def test_public_row_never_leaks_hash(self):
        row, _ = self.store.issue(role="guest", principal="frank")
        public = public_row(row)
        self.assertNotIn("token_sha256", public)
        self.assertEqual(public["principal"], "frank")

    def test_import_hash_is_idempotent(self):
        digest = hash_token("legacy-token")
        first = self.store.import_hash(role="master", principal="owner",
                                       token_hash=digest, account_id="acct_owner")
        second = self.store.import_hash(role="master", principal="owner",
                                        token_hash=digest, account_id="acct_owner")
        self.assertEqual(first["account_id"], second["account_id"])
        self.assertEqual(len(self.store.list()), 1, "重啟不該長出第二個主人帳號")


class TestAuthIntegration(unittest.TestCase):
    def build(self):
        root = pathlib.Path(tempfile.mkdtemp())
        principals = PrincipalRegistry()
        store = AccountStore(root / "accounts.json")
        guests = GuestStore(root / "guests.json")
        auth = AuthService(root / "auth.json", principals, guests,
                           owner_principal="owner", accounts=store)
        return root, store, auth

    def test_owner_token_resolves_to_master_account(self):
        _, store, auth = self.build()
        token = auth.owner_token
        self.assertTrue(token, "第一次啟動會給明文")
        ctx = auth.authenticate(f"Bearer {token}")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.role, Role.OWNER)
        self.assertEqual(ctx.token_id, "acct_owner")
        self.assertIn("acct_owner", [row["account_id"] for row in store.list()])

    def test_guest_token_resolves_to_guest_role(self):
        _, store, auth = self.build()
        _, token = store.issue(role="guest", principal="alice")
        ctx = auth.authenticate(f"Bearer {token}")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.role, Role.GUEST)
        self.assertEqual(ctx.principal, "alice")
        self.assertFalse(ctx.is_owner)

    def test_revoked_guest_cannot_authenticate(self):
        _, store, auth = self.build()
        row, token = store.issue(role="guest", principal="alice")
        store.revoke(row["account_id"])
        self.assertIsNone(auth.authenticate(f"Bearer {token}"))


class TestMeAndAdminApi(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.owner = self.state.app.auth.authenticate(
            f"Bearer {self.state.app.auth.owner_token}")

    def test_me_for_master(self):
        import agent.server.api as api
        status, body = api.get_me(self.state, auth=self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(body["role"], "master")
        self.assertEqual(body["account_id"], "acct_owner")
        self.assertTrue(body["can"]["admin"])
        self.assertTrue(body["can"]["manage_accounts"])
        self.assertGreaterEqual(len(body["entities"]), 1)
        self.assertNotIn("token", json.dumps(body))

    def test_me_for_guest_hides_admin(self):
        import agent.server.api as api
        row, token = self.state.app.accounts.issue(role="guest", principal="alice")
        guest = self.state.app.auth.authenticate(f"Bearer {token}")
        status, body = api.get_me(self.state, auth=guest)
        self.assertEqual(status, 200)
        self.assertEqual(body["role"], "guest")
        self.assertFalse(body["can"]["admin"])
        self.assertEqual(body["can"]["manage_accounts"], False)

    def test_guest_can_pick_who_to_talk_to(self):
        """guest 也拿到全部角色（聊天頁才有切換鈕），送話給別的角色也照收（owner 2026-10-29）。"""
        import agent.server.api as api
        row, token = self.state.app.accounts.issue(role="guest", principal="alice")
        guest = self.state.app.auth.authenticate(f"Bearer {token}")
        _, body = api.get_me(self.state, auth=guest)
        everyone = [item.id for item in self.state.app.entities.all()]
        self.assertEqual([item["id"] for item in body["entities"]], everyone)
        self.assertGreater(len(everyone), 1)
        status, data = api.prepare_send(self.state, {"text": "こんにちは", "entity": everyone[1]},
                                        auth=guest)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["entity"], everyone[1])
        self.assertEqual(data["mode"], "chat")

    def test_guest_bound_to_a_character_sees_only_that_one(self):
        import agent.server.api as api
        bound = [item.id for item in self.state.app.entities.all()][-1]
        row, token = self.state.app.accounts.issue(role="guest", principal=bound)
        guest = self.state.app.auth.authenticate(f"Bearer {token}")
        _, body = api.get_me(self.state, auth=guest)
        self.assertEqual([item["id"] for item in body["entities"]], [bound])

    def test_owner_can_issue_and_guest_cannot(self):
        import agent.server.api as api
        status, body = api.create_account(self.state, {"principal": "alice",
                                                       "display": "Alice"},
                                          auth=self.owner)
        self.assertEqual(status, 201)
        token = body["token"]
        self.assertTrue(token.startswith("ba_"))
        self.assertNotIn("token_sha256", json.dumps(body["account"]))

        guest_ctx = self.state.app.auth.authenticate(f"Bearer {token}")
        status, denied = api.create_account(self.state, {"principal": "eve"},
                                            auth=guest_ctx)
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "forbidden")

        status, listed = api.list_accounts(self.state, auth=self.owner)
        self.assertEqual(status, 200)
        self.assertTrue(any(row["principal"] == "alice" for row in listed["accounts"]))

    def test_rotate_and_revoke_via_api(self):
        import agent.server.api as api
        _, created = api.create_account(self.state, {"principal": "bob"}, auth=self.owner)
        account_id = created["account"]["account_id"]
        old_token = created["token"]

        status, rotated = api.rotate_account(self.state, account_id, auth=self.owner)
        self.assertEqual(status, 200)
        self.assertNotEqual(rotated["token"], old_token)
        self.assertIsNone(self.state.app.auth.authenticate(f"Bearer {old_token}"))
        self.assertIsNotNone(self.state.app.auth.authenticate(f"Bearer {rotated['token']}"))

        status, _ = api.revoke_account(self.state, account_id, auth=self.owner)
        self.assertEqual(status, 200)
        self.assertIsNone(self.state.app.auth.authenticate(
            f"Bearer {rotated['token']}"))

    def test_revoked_master_cannot_authenticate_through_legacy_hash(self):
        import agent.server.api as api
        token = self.state.app.auth.owner_token
        self.assertIsNotNone(self.state.app.auth.authenticate(f"Bearer {token}"))

        status, _ = api.revoke_account(self.state, "acct_owner", auth=self.owner)
        self.assertEqual(status, 200)
        self.assertIsNone(self.state.app.accounts.verify(token))
        self.assertIsNone(self.state.app.auth.authenticate(f"Bearer {token}"),
                          "帳號撤銷後不能再走 auth.json 的 fallback 恢復 owner")


class TestStartupMigration(unittest.TestCase):
    def test_restart_does_not_duplicate_owner_account(self):
        from agent.brain.compose.bootstrap import build
        root = pathlib.Path(tempfile.mkdtemp())
        first = build(data_dir=str(root))
        self.assertEqual(len([row for row in first.accounts.list()
                              if row["role"] == "master"]), 1)
        second = build(data_dir=str(root))
        self.assertEqual(len([row for row in second.accounts.list()
                              if row["role"] == "master"]), 1, "重啟不該多一個主人")


if __name__ == "__main__":
    unittest.main(verbosity=2)
