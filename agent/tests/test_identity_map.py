#!/usr/bin/env python3
"""核心帳號在腦端（owner 2026-11-09）：App／Discord 的外部帳號都綁在腦端 principal 上。

判據：
  ① App 的主人帳號（生產上是 `acct_owner`）進來 ⇒ 大腦看到的是 `owner`；App 的客人帳號 ⇒ 對應的客人 principal。
  ② 沒綁的 id 原樣回——**陌生人不能變成主人**（`resolve()` 會退回預設主人，這裡不能用它）。
  ③ 出站反查：主人 ⇒ 他的 App 帳號 id（開會話用）。Discord 的主人 id（`ALLOWED_USER_IDS`）也綁上。
  ④ 群聊名冊、公共區動作的人一樣翻；角色 id 兩邊同名不受影響。
  ⑤ 遷移工具預設只看；`--apply` 先備份再改事實的房間、線的名冊／說話人、主動消息的鍵。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace as NS

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.entities import build_principals
from agent.brain.compose.identity import IdentityMap, bind_identities
from agent.brain.runtime.identity.accounts import AccountStore


def app_with_accounts(root: pathlib.Path):
    accounts = AccountStore(root / "accounts.json")
    accounts.issue(role="master", principal="owner", account_id="acct_owner", display="主人")
    accounts.issue(role="guest", principal="guest-kiseki", account_id="acct_7f3a", display="輝石")
    return NS(principals=build_principals(), accounts=accounts, guest_store=None)


class TestHub(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-ident-"))
        self.app = app_with_accounts(self.root)

    def test_app_accounts_map_to_brain_principals(self) -> None:
        counts = bind_identities(self.app, env={"ALLOWED_USER_IDS": "111, 222"})
        identity = self.app.identity
        self.assertEqual(identity.principal_of("acct_owner"), "owner")
        self.assertEqual(identity.principal_of("acct_7f3a"), "guest-kiseki")
        self.assertIsNotNone(self.app.principals.get("guest-kiseki"), "App 的客人登記成腦端的客人")
        self.assertEqual(self.app.principals.get("guest-kiseki").role, "guest")
        self.assertEqual(identity.account_id_of("owner"), "acct_owner")
        self.assertEqual(self.app.principals.lookup("discord", "222"), "owner")
        self.assertGreaterEqual(counts["app"], 2)

    def test_unknown_stays_raw_never_owner(self) -> None:
        bind_identities(self.app, env={})
        identity = self.app.identity
        self.assertEqual(identity.principal_of("acct_stranger"), "acct_stranger")
        self.assertEqual(identity.principal_of("mira"), "mira", "角色兩邊同名")
        self.assertEqual(identity.account_id_of("nobody"), "nobody")
        self.assertEqual(identity.people(["acct_owner", "aster", "acct_owner"]), ["owner", "aster"])

    def test_explicit_links(self) -> None:
        self.app.accounts.link("acct_owner", "discord", "333")
        self.app.accounts.link("acct_owner", "app", "acct_owner_tablet")
        bind_identities(self.app, env={})
        self.assertEqual(self.app.principals.lookup("discord", "333"), "owner")
        self.assertEqual(self.app.identity.principal_of("acct_owner_tablet"), "owner",
                         "一個人可以綁多個 App 帳號")
        links = AccountStore(self.root / "accounts.json").links("acct_owner")
        self.assertEqual(links["discord"], ["333"], "綁定落盤")


class TestAdapterBoundary(unittest.TestCase):
    def test_owner_dm_reaches_brain_as_owner(self) -> None:
        from test_app_chat_adapter import AppChatAdapterCase, FakeClient, event

        case = AppChatAdapterCase()
        case.setUp()
        try:
            prod = event("acct_owner", text="ただいま")
            prod["payload"]["message"]["author"]["kind"] = "owner"
            prod["payload"]["participants"] = ["acct_owner", "aster"]
            adapter = case.adapter(FakeClient([prod]))
            app = app_with_accounts(pathlib.Path(tempfile.mkdtemp()))
            bind_identities(app, env={})
            adapter.identity = app.identity
            adapter.poll_once()
            self.assertEqual(case.calls[0]["principal"], "owner", "App 帳號 id 進來 ⇒ 腦端的主人")
        finally:
            case.folder.cleanup()

    def test_board_actor_named_via_hub(self) -> None:
        from agent.adapter.app_board_events import BoardEventBridge

        bridge = BoardEventBridge("aster", NS(read=lambda s: {}, write=lambda s, p: None),
                                  names={"guest-kiseki": "輝石"})
        app = app_with_accounts(pathlib.Path(tempfile.mkdtemp()))
        bind_identities(app, env={})
        bridge.identity = app.identity
        self.assertEqual(bridge._who("acct_owner"), "主人")
        self.assertEqual(bridge._who("acct_7f3a"), "輝石", "客人也認得出來（以前只寫死了主人）")


class TestMigration(unittest.TestCase):
    def build(self) -> pathlib.Path:
        data = pathlib.Path(tempfile.mkdtemp(prefix="ba-migrate-"))
        (data / "accounts.json").write_text(json.dumps({"accounts": {
            "acct_owner": {"account_id": "acct_owner", "role": "master", "principal": "owner"},
            "acct_7f3a": {"account_id": "acct_7f3a", "role": "guest", "principal": "guest-kiseki"},
        }}), encoding="utf-8")
        (data / "memory").mkdir()
        db = sqlite3.connect(str(data / "memory" / "facts.db"))
        db.execute("CREATE TABLE facts (text TEXT, principal TEXT, audience TEXT)")
        db.executemany("INSERT INTO facts VALUES (?,?,?)", [
            ("オーナーは寒いのが苦手", "acct-owner", "acct-owner,mira"),
            ("世界の事実", "", None),
            ("前から知ってる", "owner", "mira,owner"),
        ])
        db.commit()
        db.close()
        (data / "threads.json").write_text(json.dumps({"threads": [{
            "key": {"scope": "acct-owner"}, "participants": ["acct_owner", "mira"],
            "transcript": [{"speaker": "acct_owner", "addressed_to": "mira", "text": "x"}],
            "segment": []}]}), encoding="utf-8")
        (data / "proactive.json").write_text(json.dumps({
            "heard": {"mira|acct_owner": 200.0, "mira|owner": 100.0}}), encoding="utf-8")
        (data / "users").mkdir()
        (data / "users" / "acct_owner.md").write_text("# x", encoding="utf-8")
        return data

    def test_dry_run_then_apply(self) -> None:
        import contextlib
        import io
        import sys

        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "agent" / "tools"))
        import migrate_app_identities as tool

        data = self.build()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            tool.main(["--data", str(data)])
        report = json.loads(out.getvalue())
        self.assertEqual(report["facts"]["changed"], 1)
        self.assertEqual(report["threads"]["turns_changed"], 1)
        self.assertEqual(report["threads"]["scope_left_as_is"], 1)
        self.assertEqual(report["report_only"]["users_md"], [str(data / "users" / "acct_owner.md")])
        db = sqlite3.connect(str(data / "memory" / "facts.db"))
        self.assertEqual(db.execute("SELECT principal FROM facts").fetchone()[0], "acct-owner",
                         "預設只看，不改")
        db.close()
        with contextlib.redirect_stdout(io.StringIO()):
            tool.main(["--data", str(data), "--apply"])
        db = sqlite3.connect(str(data / "memory" / "facts.db"))
        self.assertEqual(db.execute("SELECT principal, audience FROM facts").fetchone(),
                         ("owner", "mira,owner"))
        db.close()
        threads = json.loads((data / "threads.json").read_text(encoding="utf-8"))["threads"][0]
        self.assertEqual(threads["participants"], ["mira", "owner"])
        self.assertEqual(threads["transcript"][0]["speaker"], "owner")
        heard = json.loads((data / "proactive.json").read_text(encoding="utf-8"))["heard"]
        self.assertEqual(heard, {"mira|owner": 200.0}, "同一個人合併、取較晚的")
        self.assertTrue(list((data / "memory").glob("facts.db.bak-identity-*")), "先備份")


if __name__ == "__main__":
    unittest.main(verbosity=2)
