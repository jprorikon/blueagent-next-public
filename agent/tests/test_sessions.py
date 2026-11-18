#!/usr/bin/env python3
"""M1 `/api/sessions`：可見角色、最新一輪、principal／來源邊界。"""

from __future__ import annotations

import unittest

from demo_common import make_state

from agent.brain.domain.auth import AuthContext, Role
from agent.server.api.sessions import list_sessions


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.state.app.shutdown_lives()  # 固定時鐘不能被背景生活回合推進
        self.owner = AuthContext("owner", Role.OWNER)
        self.entities = [item.id for item in self.state.app.entities.all()]
        self.assertEqual(len(self.entities), 3)
        self.now = 1_792_300_000.0
        self.state.app.outbox._clock = lambda: self.now

    def tearDown(self):
        self.state.app.shutdown_lives()

    def record(self, turn_id, entity, text="reply", *, principal="owner",
               asked="question", source="phone:owner", kind="", at=None):
        if at is not None:
            self.now = at
        return self.state.app.outbox.record(
            turn_id=turn_id, principal=principal, entity=entity, text=text,
            asked=asked, source=source, kind=kind)

    def get(self, auth=None):
        return list_sessions(self.state, {}, auth=self.owner if auth is None else auth)

    def test_all_visible_agents_exist_without_history(self):
        status, body = self.get()
        self.assertEqual(status, 200, body)
        self.assertEqual([row["entity"] for row in body["agents"]], self.entities)
        for row in body["agents"]:
            self.assertEqual(row["last"], "")
            self.assertEqual(row["at"], 0.0)
            self.assertEqual(row["turn_id"], "")
            self.assertEqual(row["unread"], 0, "服務端不知道端側已讀，只能給形狀佔位")
            self.assertTrue(row["name"])
        self.assertEqual(body["guests"], [])
        self.assertEqual(body["rooms"], [])

    def test_latest_text_fallback_turn_and_sorting(self):
        first, second, third = self.entities
        base = self.now
        self.record("old", third, "older", at=base + 10)
        self.record("empty", second, "", asked="only asked", at=base + 20)
        self.record("latest", first, "latest reply", asked="not shown",
                    at=base + 30)
        status, body = self.get()
        self.assertEqual(status, 200, body)
        self.assertEqual([row["entity"] for row in body["agents"]], [first, second, third])
        self.assertEqual(body["agents"][0], {
            "entity": first, "name": self.state.app.entities.get(first).name,
            "last": "latest reply", "at": base + 30,
            "turn_id": "latest", "unread": 0})
        self.assertEqual(body["agents"][1]["last"], "only asked")
        self.assertEqual(body["agents"][1]["turn_id"], "empty")
        self.assertEqual(body["agents"][1]["at"], base + 20)

    def test_principal_isolation(self):
        first, second, _ = self.entities
        self.record("owner-turn", first, "owner only")
        self.record("alice-turn", second, "alice only", principal="alice")
        _, owner = self.get()
        _, alice = self.get(AuthContext("alice"))
        self.assertEqual([row["last"] for row in owner["agents"]],
                         ["owner only", "", ""])
        self.assertEqual([row["last"] for row in alice["agents"]],
                         ["alice only", "", ""])
        self.assertNotIn("alice only", repr(owner))
        self.assertNotIn("owner only", repr(alice))

    def test_guest_bound_to_entity_only_sees_that_agent(self):
        bound = self.entities[-1]
        self.record("bound-turn", bound, "bound reply", principal=bound)
        status, body = self.get(AuthContext(bound))
        self.assertEqual(status, 200, body)
        self.assertEqual([row["entity"] for row in body["agents"]], [bound])
        self.assertEqual(body["agents"][0]["last"], "bound reply")

    def test_only_phone_and_api_non_call_activity_enters_list(self):
        first, second, third = self.entities
        self.record("discord", first, "discord", source="discord:owner", at=self.now + 1)
        self.record("mr", second, "mr", source="mr:owner", at=self.now + 2)
        self.record("call", third, "call", kind="call", at=self.now + 3)
        self.record("api", first, "api", source="api:audio", at=self.now + 4)
        _, body = self.get()
        rows = {row["entity"]: row for row in body["agents"]}
        self.assertEqual(rows[first]["last"], "api")
        self.assertEqual(rows[second]["last"], "")
        self.assertEqual(rows[third]["last"], "")

    def test_auth_permission_and_outbox_boundaries(self):
        status, body = list_sessions(self.state, {})
        self.assertEqual((status, body["error"]), (401, "unauthorized"))
        status, body = self.get(AuthContext("alice", capabilities=frozenset()))
        self.assertEqual((status, body["error"]), (403, "forbidden"))
        self.state.app.outbox = None
        status, body = self.get()
        self.assertEqual((status, body["error"]), (503, "outbox_unavailable"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
