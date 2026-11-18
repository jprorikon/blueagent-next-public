#!/usr/bin/env python3
"""帳號層匯出／匯入（換機、遷移；owner 2026-10-31 第 3 項）。

判據：
  ① 匯出**含雜湊、不含明文**，且格式有版本號；
  ② 匯入是 upsert：新增的進來、既有的沿用（token 雜湊被覆蓋成匯出那份 ⇒ 端末不必重新貼）；
  ③ 壞列跳過並說原因，**不刪任何現有帳號**；同 id 但 role 不同 ⇒ 跳過（不能被舊匯出降級）；
  ④ 只有主人能用（guest 403）。
"""

from __future__ import annotations

import hashlib
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.identity.accounts import AccountStore, hash_token


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestExportImportStore(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.store = AccountStore(self.root / "accounts.json")
        self.row, self.token = self.store.issue(role="guest", principal="alice")

    def test_export_has_hash_not_plaintext(self):
        exported = self.store.export_rows()
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["token_sha256"], hash_token(self.token))
        self.assertNotIn(self.token, str(exported), "匯出不可以有明文 token")

    def test_import_new_and_update(self):
        exported = self.store.export_rows()
        fresh = AccountStore(self.root / "other.json")
        result = fresh.merge_rows(exported)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], [])
        # 同一份再匯一次 ⇒ 變成 updated，不會長第二筆
        again = fresh.merge_rows(exported)
        self.assertEqual((again["imported"], again["updated"]), (0, 1))
        self.assertEqual(len(fresh.list(include_revoked=True)), 1)

    def test_import_replaces_hash_so_token_still_works(self):
        exported = self.store.export_rows()
        target = AccountStore(self.root / "target.json")
        target.issue(role="guest", principal="alice")          # 目標機上先有一筆（不同 token）
        target.merge_rows(exported)
        self.assertIsNotNone(target.verify(self.token), "匯入後原本端末上的 token 要能用")

    def test_bad_rows_are_skipped_with_reason(self):
        result = self.store.merge_rows([
            {"account_id": "x1", "role": "guest", "token_sha256": "abc"},          # 雜湊太短
            {"account_id": "", "role": "guest", "token_sha256": digest("t")},      # 缺 id
            {"account_id": "x3", "role": "admin", "token_sha256": digest("t")},    # 不認的 role
            "字串不是物件",
        ])
        self.assertEqual((result["imported"], result["updated"]), (0, 0))
        self.assertEqual([item["reason"] for item in result["skipped"]],
                         ["bad_token_hash", "missing_account_id", "unknown_role", "not_an_object"])
        self.assertEqual(len(self.store.list(include_revoked=True)), 1, "壞列不能動到既有帳號")

    def test_role_conflict_is_skipped(self):
        result = self.store.merge_rows([{"account_id": self.row["account_id"], "role": "master",
                                         "token_sha256": digest("t")}])
        self.assertEqual(result["skipped"], [{"account_id": self.row["account_id"],
                                             "reason": "role_conflict"}])
        self.assertEqual(self.store.get(self.row["account_id"])["role"], "guest", "不能被降級/升級")

    def test_merge_never_deletes(self):
        exported = self.store.export_rows()
        other = AccountStore(self.root / "keep.json")
        kept, _ = other.issue(role="guest", principal="keep-me")
        other.merge_rows(exported)
        self.assertIsNotNone(other.get(kept["account_id"]), "匯入不能刪掉本機既有的帳號")


class TestExportImportApi(unittest.TestCase):
    def setUp(self) -> None:
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.owner = self.state.app.auth.authenticate(
            f"Bearer {self.state.app.auth.owner_token}")

    def test_export_requires_owner(self):
        import agent.server.api as api
        row, token = self.state.app.accounts.issue(role="guest", principal="alice")
        guest = self.state.app.auth.authenticate(f"Bearer {token}")
        status, _ = api.export_accounts(self.state, auth=guest)
        self.assertEqual(status, 403)
        status, body = api.export_accounts(self.state, auth=self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(body["format"], "blueagent-accounts/1")
        self.assertNotIn(token, str(body))

    def test_import_round_trip(self):
        import agent.server.api as api
        _, body = api.export_accounts(self.state, auth=self.owner)
        status, result = api.import_accounts(self.state, {"accounts": body["accounts"]},
                                             auth=self.owner)
        self.assertEqual(status, 200, result)
        self.assertGreaterEqual(result["total"], 1)
        self.assertEqual(result["skipped"], [])

    def test_import_rejects_garbage(self):
        import agent.server.api as api
        status, body = api.import_accounts(self.state, {"accounts": "not a list"}, auth=self.owner)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "accounts_required")


if __name__ == "__main__":
    unittest.main()
