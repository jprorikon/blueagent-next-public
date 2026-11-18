#!/usr/bin/env python3
"""公共区事件回流（owner 2026-11-07：「事件回流做」）。

判据：
  ① 只回流**她自己的帖**上发生的事（现查作者，不靠「她发过哪些帖」的记忆）；
     她自己的动作（自己赞／自己回）不回流；
  ② 一条世界事件、一句话，不开 LLM 回合、不自动回帖；
  ③ 同一波（5 分钟）同一个人同一种动作只报一次；每角色每天 40 条上限；
  ④ 发帖事件本身不回流；查不到帖（网络失败）宁可跳过也不误报。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent" / "tests"))

from agent.adapter.app_adapter_store import AdapterSidecar
from agent.adapter.app_board_events import NOTICE_PER_DAY, BoardEventBridge
from agent.adapter.app_server import AppChatAdapter


def event(kind, *, actor="owner", post=None, post_id="", event_id="evt_b1", cursor="5"):
    payload = {}
    if post is not None:
        payload["post"] = post
    if post_id:
        payload["post_id"] = post_id
    return {"event_id": event_id, "cursor": str(cursor), "type": kind,
            "actor": {"id": actor, "kind": "owner" if actor == "owner" else "entity"},
            "payload": payload}


def own_post(ident="pst_mine", body="今日は静かだった"):
    return {"id": ident, "author_id": "aster", "author_kind": "entity",
            "body": body, "parent_id": "", "at": 1794060000.0}


class BridgeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-board-events-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.now = [1794060000.0]
        self.sidecar = AdapterSidecar(self.root / "aster-cursor.json")
        self.bridge = BoardEventBridge("aster", self.sidecar, {"aster": "アスター", "nori": "ノリ"},
                                       clock=lambda: self.now[0])
        self.notices = []

    def lookup(self, post_id):
        """她自己的帖（默认全算她的）。"""
        return {"post": own_post(post_id)}

    def foreign_lookup(self, post_id):
        row = own_post(post_id)
        row["author_id"] = "nori"
        return {"post": row}

    def broken_lookup(self, post_id):
        raise RuntimeError("app_server_http_502")

    def feed_event(self, row):
        notice = self.bridge.consider(row, lookup=self.lookup)
        if notice:
            self.notices.append(notice)
        return notice

    def test_post_created_never_announces_anything(self):
        self.assertIsNone(self.feed_event(event("post.created", actor="aster",
                                                post=own_post())))
        self.assertEqual(self.notices, [])

    def test_reply_on_her_post_becomes_one_notice(self):
        notice = self.feed_event(event("reply.created", actor="nori", post={
            "id": "pst_r1", "author_id": "nori", "parent_id": "pst_mine",
            "body": "それ、いいね"}))
        self.assertIsNotNone(notice)
        self.assertEqual(notice["kind"], "board.reply")
        self.assertEqual(notice["post_id"], "pst_mine")
        self.assertIn("ノリ", notice["text"])
        self.assertIn("それ、いいね", notice["text"])
        self.assertIn("今日は静かだった", notice["text"])

    def test_reply_on_someone_else_post_is_ignored(self):
        notice = self.bridge.consider(
            event("reply.created", actor="mira", post={
                "id": "pst_r2", "author_id": "mira", "parent_id": "pst_other",
                "body": "x"}), lookup=self.foreign_lookup)
        self.assertIsNone(notice, "别人的帖被回，不该回流成她的事")

    def test_unknown_post_is_skipped_not_guessed(self):
        """查不到帖就不报（宁可不报也不误报她「被回了」）。"""
        notice = self.bridge.consider(
            event("post.liked", post_id="pst_mine"), lookup=self.broken_lookup)
        self.assertIsNone(notice)

    def test_like_on_her_post_is_announced_and_deduped(self):
        self.assertIsNotNone(self.feed_event(event("post.liked", post_id="pst_mine")))
        self.now[0] += 60                                     # 5 分钟内同一波
        self.assertIsNone(self.feed_event(event("post.liked", post_id="pst_mine")))
        self.now[0] += 600                                    # 窗口过了才再报
        self.assertIsNotNone(self.feed_event(event("post.liked", post_id="pst_mine")))

    def test_her_own_like_or_reply_never_comes_back(self):
        self.assertIsNone(self.feed_event(event("post.liked", actor="aster", post_id="pst_mine")))

    def test_daily_cap_stops_flooding(self):
        sent = 0
        for index in range(NOTICE_PER_DAY + 5):
            self.now[0] += 1
            if self.feed_event(event("post.liked", actor=f"guest{index}",
                                     post_id="pst_mine", event_id=f"evt_{index}")):
                sent += 1
        self.assertEqual(sent, NOTICE_PER_DAY)

    def test_counter_rolls_over_next_day(self):
        self.feed_event(event("post.liked", post_id="pst_mine"))
        self.now[0] += 86400 + 60
        self.assertIsNotNone(self.feed_event(event("post.liked", post_id="pst_mine")))


class _BoardClient:
    """只回公共区事件的最小客户端（够验 adapter 的分流）。"""

    def __init__(self, rows):
        self.rows = list(rows)
        self.board_calls = []

    def events(self, cursor, limit=100):
        return {"events": [dict(row) for row in self.rows], "next_cursor": "9", "has_more": False}

    def board_call(self, name, arguments):
        self.board_calls.append((name, dict(arguments)))
        return {"post": own_post(str(arguments.get("id") or "")), "replies": []}


class AdapterBoardEventCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-adapter-board-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.cursor = self.root / "aster-cursor.json"
        self.cursor.write_text(json.dumps({"cursor": "0"}), encoding="utf-8")
        self.calls = []
        self.notices = []

    def adapter(self, client):
        return AppChatAdapter("aster", client, lambda text, **kwargs: self.calls.append(kwargs) or {},
                              self.cursor, lambda result: "", lambda result: [],
                              log=lambda _m: None,
                              on_board=lambda entity, kind, notice, _event:
                              self.notices.append((entity, kind, notice["post_id"])),
                              aliases={"aster": ("aster", "アスター")}, names={"aster": "アスター", "nori": "ノリ"})

    def test_board_event_turns_into_one_notice_and_no_turn(self):
        client = _BoardClient([event("reply.created", actor="nori",
                                     post={"id": "pst_r9", "author_id": "nori",
                                           "parent_id": "pst_mine", "body": "見たよ"},
                                     event_id="evt_r9", cursor="9")])
        adapter = self.adapter(client)
        self.assertEqual(adapter.poll_once(), 1)
        self.assertEqual(self.calls, [], "回流不开 LLM 回合")
        self.assertEqual(self.notices, [("aster", "board.reply", "pst_mine")])
        self.assertEqual(client.board_calls, [("get", {"id": "pst_mine"})],
                         "要现查帖的作者，才知道是不是她的帖")
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "9")


if __name__ == "__main__":
    unittest.main(verbosity=2)
