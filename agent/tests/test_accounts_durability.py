#!/usr/bin/env python3
"""帳號層「壞檔不能靜默變成空庫」與「備份要含帳號檔」的回歸測試。

背景（2026-10-31 owner 回報）：重建容器之後帳號只剩 owner 與 guest-707，App 裡建的 guest 消失。
機制：`AuthService._migrate_legacy` 在啟動時把 owner 與 `guests.json` 那批舊 guest 收編成帳號；
**App 建的 guest 只存在 `accounts.json`**。所以只要那個檔在啟動時讀不出來（或不存在），
它就會被 legacy 名單覆蓋 ⇒ 永久消失，而且舊版 `_load` 是「靜靜 return」。

判據：
  ① 壞檔被改名成 `.corrupt-<時間>`（證據不丟）、當次以空庫啟動、且**有記錄**；
  ② 格式不對（沒有 accounts 物件）同樣走留證路徑；
  ③ 正常檔照舊載入（不誤判）；
  ④ 每日備份清單包含 accounts.json／guests.json／auth.json。

2026-10-31 追加（同一天被誤隔離 12 次）：**「讀不到」不等於「檔壞了」**。
容器以 root 寫 0600，主機側工具以 986 讀 ⇒ PermissionError，舊版照樣改名隔離、以空庫啟動。
判據：
  ⑤ 讀不到（權限）⇒ **不改名、不覆蓋**，進唯讀模式；
  ⑥ 存檔用的暫存檔名每個 process 唯一（固定 `.tmp` 會被兩個 writer 互相截斷）。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.identity.accounts import AccountStore
from agent.brain.runtime.ops.backup import DataBackup


class TestCorruptAccountsFile(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.path = self.root / "accounts.json"
        self.messages: list[str] = []

    def test_broken_json_is_quarantined_not_overwritten(self):
        self.path.write_text('{"version": 1, "accounts": {"acct_x": ', encoding="utf-8")   # 截斷的檔
        store = AccountStore(self.path, log=self.messages.append)
        self.assertEqual(store.list(include_revoked=True), [])          # 這次是空庫
        self.assertFalse(self.path.exists(), "壞檔不該留在原名（會被下一次存檔覆蓋）")
        kept = list(self.root.glob("accounts.json.corrupt-*"))
        self.assertEqual(len(kept), 1, "壞檔要改名留證")
        self.assertIn("acct_x", kept[0].read_text(encoding="utf-8"))     # 原內容還在
        self.assertTrue(any("accounts: 讀不出" in message for message in self.messages),
                        f"要留下記錄，實際：{self.messages}")

    def test_wrong_shape_is_quarantined(self):
        self.path.write_text('{"version": 1}', encoding="utf-8")
        AccountStore(self.path, log=self.messages.append)
        self.assertEqual(len(list(self.root.glob("accounts.json.corrupt-*"))), 1)

    def test_healthy_file_still_loads(self):
        row = {"account_id": "acct_owner", "role": "master", "principal": "owner",
               "display": "owner", "capabilities": ["chat"], "token_sha256": "x" * 64,
               "created_at": 1.0, "last_seen_at": 0.0, "revoked_at": 0.0, "note": "imported"}
        self.path.write_text(json.dumps({"version": 1, "accounts": {"acct_owner": row}}),
                             encoding="utf-8")
        store = AccountStore(self.path, log=self.messages.append)
        self.assertEqual([item["account_id"] for item in store.list(include_revoked=True)],
                         ["acct_owner"])
        self.assertEqual(self.messages, [])
        self.assertFalse(list(self.root.glob("accounts.json.corrupt-*")))

    def test_missing_file_is_not_quarantine(self):
        AccountStore(self.path, log=self.messages.append)
        self.assertEqual(self.messages, [])
        self.assertFalse(list(self.root.glob("accounts.json.corrupt-*")))


class TestUnreadableIsNotCorrupt(unittest.TestCase):
    """讀不到 ≠ 壞了：權限問題不能把好檔改名，也不能拿空庫去覆蓋它。"""

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.path = self.root / "accounts.json"
        self.messages: list[str] = []
        row = {"account_id": "acct_owner", "role": "master", "principal": "owner",
               "display": "owner", "capabilities": ["chat"], "token_sha256": "x" * 64,
               "created_at": 1.0, "last_seen_at": 0.0, "revoked_at": 0.0, "note": ""}
        self.path.write_text(json.dumps({"version": 1, "accounts": {"acct_owner": row}}),
                             encoding="utf-8")
        self.original = self.path.read_text(encoding="utf-8")

    def tearDown(self) -> None:
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def test_permission_error_keeps_the_file_and_goes_readonly(self):
        self.path.chmod(0o000)                       # 非 root 的 owner 自己也會被 DAC 擋住
        store = AccountStore(self.path, log=self.messages.append)
        self.assertEqual(store.list(include_revoked=True), [], "這次讀不到 ⇒ 手上沒有名單")
        self.assertTrue(store.readonly)
        self.assertTrue(self.path.exists(), "**不可以改名**：好檔還在，只是這次讀不到")
        self.assertFalse(list(self.root.glob("accounts.json.corrupt-*")), "不是壞檔，不該留證")
        self.assertTrue(any("唯讀" in message for message in self.messages),
                        f"要大聲說進唯讀模式：{self.messages}")
        # 還讀不到時的存檔一律不動原檔（不然空庫會把帳號吃掉）
        store.issue(role="guest", principal="acct-new-guest")
        self.path.chmod(0o600)                      # 還原權限，才讀得回來檢查
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)

    def test_readonly_recovers_and_merges_once_readable(self):
        """權限修好之後不用重啟：下一次存檔／驗證就把原檔讀回來、併上唯讀期間新簽的，再寫回。"""
        clock = [1000.0]
        self.path.chmod(0o000)
        store = AccountStore(self.path, log=self.messages.append, clock=lambda: clock[0])
        _row, token = store.issue(role="guest", principal="during-readonly")   # 讀不到：只在記憶體
        self.assertTrue(store.readonly)
        self.path.chmod(0o600)
        # 原檔裡的主人 token 這時還驗不到（手上沒有那一列）⇒ 節流內不重讀
        self.assertIsNone(store.verify("not-a-real-token"))
        clock[0] += store.RECOVER_EVERY_S + 1
        store.issue(role="guest", principal="after-fix")
        self.assertFalse(store.readonly, "讀得到了就要解除唯讀")
        saved = json.loads(self.path.read_text(encoding="utf-8"))["accounts"]
        principals = sorted(row["principal"] for row in saved.values())
        self.assertEqual(principals, ["after-fix", "during-readonly", "owner"],
                         "原檔的帳號一個都不能少，唯讀期間新簽的也要留下")
        self.assertIsNotNone(store.verify(token), "唯讀期間發出去的 token 照樣能用")

    def test_verify_recovers_missing_accounts(self):
        """唯讀中驗不到 ⇒ 試著讀回原檔再驗（不然 App 建的 guest 要 401 到下次重啟）。"""
        import hashlib
        token = "ba_" + "1" * 48
        row = {"account_id": "acct_g_app", "role": "guest", "principal": "app-guest",
               "display": "app-guest", "capabilities": ["chat"],
               "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
               "created_at": 1.0, "last_seen_at": 0.0, "revoked_at": 0.0, "note": ""}
        body = json.loads(self.original)
        body["accounts"]["acct_g_app"] = row
        self.path.write_text(json.dumps(body), encoding="utf-8")
        self.path.chmod(0o000)
        store = AccountStore(self.path, log=self.messages.append)
        self.path.chmod(0o600)
        found = store.verify(token, touch=False)
        self.assertIsNotNone(found, "原檔讀得到了就要認得 App 建的 guest")
        self.assertEqual(found["principal"], "app-guest")
        self.assertFalse(store.readonly)


class TestTempFileIsUniquePerProcess(unittest.TestCase):
    def test_two_stores_do_not_clobber_a_shared_tmp(self):
        root = pathlib.Path(tempfile.mkdtemp())
        path = root / "accounts.json"
        first = AccountStore(path)
        second = AccountStore(path)
        for index in range(20):
            first.issue(role="guest", principal=f"g{index}")       # 各自存檔
            second.issue(role="guest", principal=f"h{index}")
        body = json.loads(path.read_text(encoding="utf-8"))         # 一定要還是合法 JSON
        self.assertIn("accounts", body)
        self.assertFalse(list(root.glob("*.tmp")), "暫存檔不能留在目錄裡")


class TestBackupCoversAccounts(unittest.TestCase):
    def test_targets_include_account_layer(self):
        root = pathlib.Path(tempfile.mkdtemp())
        for name in ("accounts.json", "guests.json", "auth.json"):
            (root / name).write_text("{}", encoding="utf-8")
        backup = DataBackup(root, root=root / "backups")
        targets = [path.name for path in backup._targets()]
        for name in ("accounts.json", "guests.json", "auth.json"):
            self.assertIn(name, targets, f"備份要含 {name}（不然帳號丟了就救不回來）")


if __name__ == "__main__":
    unittest.main()
