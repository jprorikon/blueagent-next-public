#!/usr/bin/env python3
"""guest 不能讓她進 TRPG 桌（owner 2026-10-29「不要讓 guest 控制 agent 加入 trpg」）。

判據：
  ① TRPG 整組 API（看桌列表／一桌摘要／進桌／說話／行動）：guest ⇒ 403，而且桌那邊一個呼叫都沒發出去
     （看桌也是拿她的 hub 帳號去問的，不能借給 guest）；
  ② 主人照舊都能用；
  ③ 沒 token ⇒ 401；`/api/me` 的 `can.trpg` 跟著角色走。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.auth import AuthContext, Role
from agent.server import api as api_module

OWNER = AuthContext(principal="owner", role=Role.OWNER, capabilities=frozenset({"chat"}))
GUEST = AuthContext(principal="guest-x", role=Role.GUEST, capabilities=frozenset({"chat"}))


class FakeTables:
    """記下每一個打到桌那邊的呼叫（不連 hub）。"""

    def __init__(self):
        self.calls: list[str] = []

    def tables(self, entity):
        self.calls.append("tables")
        return [{"id": "t1"}]

    def token_available(self, entity):
        return True

    def table(self, entity, table_id):
        self.calls.append("table")
        return {"id": table_id}

    def enter(self, entity, table_id, char_id=""):
        self.calls.append("enter")
        return {"session": "s1", "seat": "a"}

    def say(self, entity, table_id, text, seat="", session=""):
        self.calls.append("say")
        return {"ok": True}

    def act(self, entity, table_id, action, **kwargs):
        self.calls.append("act")
        return {"ok": True}


class TrpgGuestCase(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.tables = FakeTables()
        self.state.trpg_service = self.tables

    def drive(self, auth):
        return [
            api_module.get_trpg_tables(self.state, {"entity": ["mira"]}, auth=auth)[0],
            api_module.get_trpg_table(self.state, {"id": ["t1"]}, auth=auth)[0],
            api_module.post_trpg_enter(self.state, {"table": "t1", "entity": "mira"}, auth=auth)[0],
            api_module.post_trpg_say(self.state, {"table": "t1", "text": "やあ"}, auth=auth)[0],
            api_module.post_trpg_act(self.state, {"table": "t1", "action": "move"}, auth=auth)[0],
        ]


class TestGuestCannotDrive(TrpgGuestCase):
    def test_guest_is_refused_before_the_table_is_touched(self):
        self.assertEqual(self.drive(GUEST), [403] * 5)
        self.assertEqual(self.tables.calls, [], "403 之前不能已經叫她進桌")

    def test_owner_still_drives(self):
        self.assertEqual(self.drive(OWNER), [200] * 5)
        self.assertEqual(self.tables.calls, ["tables", "table", "enter", "say", "act"])

    def test_no_token_is_401(self):
        self.assertEqual(self.drive(None), [401] * 5)


class TestMeSaysSo(TrpgGuestCase):
    def test_can_trpg_follows_the_role(self):
        _, owner = api_module.get_me(self.state, auth=OWNER)
        _, guest = api_module.get_me(self.state, auth=GUEST)
        self.assertTrue(owner["can"]["trpg"])
        self.assertFalse(guest["can"]["trpg"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
