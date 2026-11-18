#!/usr/bin/env python3
"""帳號層「單一真源」與體檢項的回歸測試（owner 2026-10-31：「現在做 1、2」）。

① `GuestStore.remember_account`：App 簽發的 guest 也要留在 `guests.json`，且同一 principal 不長第二筆；
② 端到端：`accounts.json` 被清空後，啟動遷移能從 `guests.json` 把帳號重建回來；
③ 體檢項 `accounts-vs-guests`：approved 名單有人卻沒帳號 ⇒ WARN（重建時會消失）。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.guest import hash_guest_token  # noqa: E402
from agent.brain.domain.principal import PrincipalRegistry
from agent.brain.runtime.identity.accounts import AccountStore
from agent.brain.runtime.identity.auth import AuthService
from agent.brain.runtime.doctor import Ctx, _accounts_cover_approved_guests
from agent.brain.runtime.identity.guest_store import GuestStore


class TestRememberAccount(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.guests = GuestStore(self.root / "guests.json")

    def test_remember_then_idempotent(self):
        first = self.guests.remember_account(principal_id="guest-a", token="gt_aaa",
                                            display_name="A")
        self.assertEqual(first.status.value, "approved")
        self.assertEqual([item.principal_id for item in self.guests.accounts()], ["guest-a"])

        again = self.guests.remember_account(principal_id="guest-a", token="gt_bbb")
        self.assertEqual(again.id, first.id, "同一個 principal 不該長第二筆")
        self.assertEqual(again.token_sha256, hash_guest_token("gt_bbb"), "要更新成新的 token（只存雜湊）")
        self.assertEqual(again.token, "", "明文不落盤")
        self.assertEqual(len(self.guests.accounts()), 1)

    def test_remember_survives_reload(self):
        self.guests.remember_account(principal_id="guest-b", token="gt_ccc", display_name="B")
        reloaded = GuestStore(self.root / "guests.json")
        rows = [item for item in reloaded.accounts() if item.principal_id == "guest-b"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].token_sha256, hash_guest_token("gt_ccc"))
        self.assertEqual(rows[0].token, "")


class TestMigrationRebuildsFromGuests(unittest.TestCase):
    """帳號檔被清空（或壞掉）時，啟動遷移要把 guests.json 的人重建回來。"""

    def test_empty_accounts_is_rebuilt(self):
        root = pathlib.Path(tempfile.mkdtemp())
        guests = GuestStore(root / "guests.json")
        guests.remember_account(principal_id="guest-c", token="gt_ddd", display_name="C")
        principals = PrincipalRegistry()
        auth = AuthService(root / "auth.json", principals, guests,
                           owner_principal="owner",
                           accounts=AccountStore(root / "accounts.json"))
        assert auth.owner_token or True                       # 觸發 owner token 建立與遷移
        rows = AccountStore(root / "accounts.json").list(include_revoked=True)
        principals_seen = {row["principal"] for row in rows}
        self.assertIn("owner", principals_seen)
        self.assertIn("guest-c", principals_seen, "guests.json 裡的人要在重建後回來")


class TestCreateAccountMirrorsToGuests(unittest.TestCase):
    """API 签發 guest 時，`guests.json` 也要有他（不然重建容器就消失）。"""

    def setUp(self) -> None:
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.owner = self.state.app.auth.authenticate(
            f"Bearer {self.state.app.auth.owner_token}")

    def test_create_account_marks_guest_approved(self):
        import agent.server.api as api
        status, body = api.create_account(
            self.state, {"role": "guest", "principal": "guest-mirror"}, auth=self.owner)
        self.assertEqual(status, 201, body)
        approved = [item.principal_id for item in self.state.app.auth.guests.accounts()]
        self.assertIn("guest-mirror", approved)
        self.assertEqual(approved.count("guest-mirror"), 1, "同一個 principal 不該長第二筆")


class TestDoctorAccountsVsGuests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.ctx = Ctx(self.root, "mira")

    def _write(self, accounts: dict, approved: list[str], *, status: str = "approved") -> None:
        (self.root / "accounts.json").write_text(
            json.dumps({"version": 1, "accounts": accounts}), encoding="utf-8")
        (self.root / "guests.json").write_text(
            json.dumps({"requests": [{"id": f"r{index}", "principal_id": pid, "status": status}
                                     for index, pid in enumerate(approved)]}), encoding="utf-8")

    def test_missing_account_warns(self):
        self._write({"acct_owner": {"role": "master", "principal": "owner"}},
                    ["guest-707", "guest-x"])
        status, detail = _accounts_cover_approved_guests(self.ctx)
        self.assertEqual(status, "warn")
        self.assertIn("guest-x", detail)

    def test_covered_is_ok(self):
        self._write({"acct_owner": {"role": "master", "principal": "owner"},
                     "acct_g": {"role": "guest", "principal": "guest-707"}},
                    ["guest-707"])
        status, detail = _accounts_cover_approved_guests(self.ctx)
        self.assertEqual(status, "ok", detail)

    def test_pending_requests_are_ignored(self):
        self._write({"acct_owner": {"role": "master", "principal": "owner"}},
                    ["guest-waiting"], status="pending")
        status, _ = _accounts_cover_approved_guests(self.ctx)
        self.assertEqual(status, "ok")


if __name__ == "__main__":
    unittest.main()
