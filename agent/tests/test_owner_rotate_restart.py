"""主人在 App 輪替自己的 token ⇒ 重啟後服務還起得來、新 token 仍是主人（2026-10-31 複檢）。

舊行為（`4539bac` 之後）：`rotate_account` 不分角色就同步 `guests.json` ⇒ 主人被記成一個 approved guest
（還帶明文 token）；下次啟動 `restore_guests` 建 owner→owner 關係拋 `ValueError`，整個服務起不來。
另外主人輪替只換 accounts.json、不換 auth.json ⇒ 重啟被蓋回舊 token、新 token 失效。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.bootstrap import build
from agent.brain.compose.state import AppState
from agent.brain.domain.auth import Role
from agent.server import api


def boot(data: str) -> AppState:
    demo = build(brain_delay=0.0, data_dir=data, step_duration=0.02, life_pause=0.005)
    demo.principals.register(Principal("owner", "主人", owner=True), replace=True)
    return AppState(demo)


def role_of(state: AppState, token: str):
    ctx = state.app.auth.authenticate("Bearer " + token)
    return ctx.role if ctx is not None else None


class TestOwnerRotateRestart(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="ba-owner-rot-")

    def test_rotate_owner_then_restart(self):
        state = boot(self.data)
        old = state.app.auth.owner_token
        status, body = api.rotate_account(state, "acct_owner", auth=state.app.auth.owner_context())
        self.assertEqual(status, 200, body)
        new = body["token"]
        self.assertEqual(body["account"]["role"], "master")
        self.assertEqual(role_of(state, new), Role.OWNER)
        if old:
            self.assertIsNone(role_of(state, old))                 # 舊的立刻失效
        guests = json.loads((pathlib.Path(self.data) / "guests.json").read_text(encoding="utf-8"))
        self.assertFalse([row for row in guests.get("requests", [])
                          if row.get("principal_id") == "owner"], "主人不該被記進 guests.json")
        state.app.shutdown_lives()

        again = boot(self.data)                                        # 以前這裡拋 ValueError
        self.addCleanup(again.app.shutdown_lives)
        self.assertEqual(role_of(again, new), Role.OWNER)              # 重啟後新 token 仍是主人
        if old:
            self.assertIsNone(role_of(again, old))

    def test_server_still_boots_with_owner_already_in_guests_json(self):
        """已經被舊版寫壞的 guests.json（裡面有一個「owner」guest）也要能開機，而且不收編成 guest。"""
        state = boot(self.data)
        token = "ot_leaked_owner_token_from_old_build_0123456789"
        state.app.auth.guests.remember_account(principal_id="owner", token=token, display_name="owner")
        state.app.shutdown_lives()

        again = boot(self.data)
        self.addCleanup(again.app.shutdown_lives)
        self.assertNotEqual(role_of(again, token), Role.GUEST)

    def test_guest_rotate_still_syncs_guests_json(self):
        state = boot(self.data)
        self.addCleanup(state.app.shutdown_lives)
        owner = state.app.auth.owner_context()
        status, created = api.create_account(state, {"principal": "alice", "display": "Alice"}, auth=owner)
        self.assertEqual(status, 201, created)
        account_id = created["account"]["account_id"]
        _, rotated = api.rotate_account(state, account_id, auth=owner)
        request = next(row for row in state.app.auth.guests.accounts() if row.principal_id == "alice")
        from agent.brain.domain.guest import hash_guest_token
        self.assertEqual(request.token_sha256, hash_guest_token(rotated["token"]))   # legacy 那條路也換成新 token
        self.assertEqual(request.token, "")                                        # 明文不落盤
        self.assertIsNone(role_of(state, created["token"]))

    def test_importing_an_old_master_token_survives_restart(self):
        """換機：匯入舊機器的主人雜湊 ⇒ 舊機器的主人 token 生效，而且重啟後不被 auth.json 蓋回去。"""
        from agent.brain.runtime.identity.accounts import hash_token
        state = boot(self.data)
        owner = state.app.auth.owner_context()
        old_machine_token = "ot_old_machine_owner_token_0123456789abcdef"
        status, body = api.import_accounts(state, {"accounts": [{
            "account_id": "acct_owner", "role": "master", "principal": "owner",
            "token_sha256": hash_token(old_machine_token)}]}, auth=owner)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["master_replaced"])
        self.assertIn("warning", body)
        self.assertEqual(role_of(state, old_machine_token), Role.OWNER)
        state.app.shutdown_lives()

        again = boot(self.data)
        self.addCleanup(again.app.shutdown_lives)
        self.assertEqual(role_of(again, old_machine_token), Role.OWNER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
