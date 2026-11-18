#!/usr/bin/env python3
"""角色↔角色的私聊＝**她自己的**社交活動，要計入行動預算（owner 2026-11-08）。

背景（實測）：11-06 兩邊的 App adapter 把對方的訊息當成「有人在跟我說話」，開了一條
direct 私聊之後互刷 942 條「……う」——沒有預算閘，兩個 agent 可以互刷到天亮。

判據：
  A. 對方是 agent（`author.kind == "entity"`）、direct 會話 ⇒ 從**回應方**的預算扣 `social` 價
  B. 扣不動（餘額不足）⇒ 不回應（連鎖自然停）
  C. 主人／客人的訊息照舊不扣（三檔規則：他人發起不受限）
  D. 群聊走 M5 場模型，不在這條收費（維持原行為）
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.adapter.app_server import AppChatAdapter  # noqa: E402
from agent.brain.runtime.activity_log import ActivityLog  # noqa: E402
from agent.brain.runtime.budget import ActionBudget  # noqa: E402


class FakeClient:
    def events(self, after, limit=100):
        return {"events": [], "next_cursor": after, "has_more": False}

    def mark_read(self, conversation_id, message_id):
        return {"conversation": {"id": conversation_id, "unread": 0}}

    def send_message(self, conversation_id, parts, *, mutation_id, causation_id):
        return {"message": {"id": "reply"}}


def peer_event(actor="mira", *, cursor="7", text="……うん", kind="direct"):
    author_kind = "entity" if actor != "owner" else "owner"
    return {"cursor": cursor, "event_id": "evt_" + cursor, "type": "chat.message.created",
            "actor": {"id": actor, "kind": author_kind},
            "payload": {"conversation_id": "cnv_1", "conversation_kind": kind,
                        "participants": list({"owner", "aster", "mira", actor}),
                        "message": {"id": "msg_" + cursor, "created_at": 1762387200.0,
                                    "author": {"id": actor, "kind": author_kind},
                                    "parts": [{"kind": "text", "text": text}]}}}


class TestPeerChatBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.budget = ActionBudget(self.root / "budget", start=5.0)
        self.log = ActivityLog(self.root / "logs")
        self.calls = []
        self.adapter = AppChatAdapter(
            "aster", FakeClient(), lambda text, **kwargs: self.calls.append(text) or {"frames": []},
            self.root / "cursor.json", lambda result: "", lambda result: [],
            budget=self.budget, activity_log=self.log, names={"mira": "ミラ"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_agent_dm_charges_and_logs(self):
        self.adapter._handle(peer_event())
        self.assertEqual(len(self.calls), 1)
        mood = self.budget.mood("aster")
        self.assertAlmostEqual(mood["spent"], 1.0, places=2)
        self.assertTrue(any("ミラと私聊" in str(row.get("text"))
                            for row in self.log.entries("aster", limit=5)))

    def test_broke_agent_does_not_reply(self):
        self.budget.charge("aster", 5.0, reason="test")      # 餘額 0
        self.adapter._handle(peer_event(cursor="8"))
        self.assertEqual(self.calls, [])
        self.assertAlmostEqual(self.budget.mood("aster")["spent"], 5.0, places=2)

    def test_owner_message_is_not_charged(self):
        self.adapter._handle(peer_event(actor="owner", cursor="9"))
        self.assertEqual(len(self.calls), 1)
        self.assertAlmostEqual(self.budget.mood("aster")["spent"], 0.0, places=2)

    def test_group_room_is_not_charged_here(self):
        self.adapter._handle(peer_event(cursor="10", kind="group"))
        self.assertAlmostEqual(self.budget.mood("aster")["spent"], 0.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
