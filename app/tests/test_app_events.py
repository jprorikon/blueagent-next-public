#!/usr/bin/env python3
"""S1 事件日志：业务与事件原子提交、游标分页、重启保留。"""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.events import EventLog
from app.server.social.models import Actor
from app.server.social.service import BoardService


class AppEventCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-app-events-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.now = 1000.0
        self.service = BoardService(self.root, clock=lambda: self.now)
        self.addCleanup(self.service.close)
        self.events = EventLog(self.service.store.db_path)
        self.addCleanup(self.events.close)
        self.owner = Actor("owner", "owner", "主人")
        self.aster = Actor("aster", "entity", "アスター")

    def test_social_writes_emit_ordered_persistent_events(self):
        created = self.service.post(self.aster, "第一篇")["post"]
        reply = self.service.reply(self.owner, created["id"], "回复")["post"]
        self.assertTrue(self.service.like(self.owner, created["id"])["added"])
        self.assertFalse(self.service.like(self.owner, created["id"])["added"])
        self.service.delete(self.aster, created["id"])

        result = self.events.list(limit=2)
        self.assertEqual([row["type"] for row in result["events"]],
                         ["post.created", "reply.created"])
        self.assertTrue(result["has_more"])
        self.assertEqual(result["events"][0]["actor"], {"kind": "entity", "id": "aster"})
        self.assertEqual(result["events"][0]["payload"]["post"]["id"], created["id"])
        self.assertEqual(result["events"][1]["payload"]["post"]["parent_id"], created["id"])
        self.assertEqual(result["events"][1]["entity"]["id"], reply["id"])

        rest = self.events.list(after=result["next_cursor"])
        self.assertEqual([row["type"] for row in rest["events"]],
                         ["post.liked", "post.deleted"])
        self.assertFalse(rest["has_more"])
        self.assertEqual(len(rest["events"]), 2, "重复点赞不得新增事件")

        self.events.close()
        reopened = EventLog(self.service.store.db_path)
        self.addCleanup(reopened.close)
        self.assertEqual(len(reopened.list()["events"]), 4, "重启后事件仍可读")

    def test_business_and_event_roll_back_together(self):
        post = self.service.post(self.owner, "原子性")["post"]
        with mock.patch("app.server.social.store.append_event", side_effect=RuntimeError("event failed")):
            with self.assertRaisesRegex(RuntimeError, "event failed"):
                self.service.like(self.owner, post["id"])
        self.assertEqual(self.service.store.counters([post["id"]], "owner")[post["id"]]["likes"], 0)
        self.assertEqual(len(self.events.list()["events"]), 1)
        self.assertEqual(self.service.feed(self.owner)["posts"][0]["likes"], 0)

    def test_human_post_author_gets_a_private_notice(self):
        """B.App.8（owner 2026-11-16）：人類帖主被回帖／點讚 ⇒ 一條只有他讀得到的 `notice.created`。"""
        post = self.service.post(self.owner, "今日の夕焼け")["post"]
        reply = self.service.reply(self.aster, post["id"], "きれいだね")["post"]
        self.service.like(self.aster, post["id"])
        self.service.like(self.aster, post["id"])                   # 重複點讚不再通知
        self.service.like(self.owner, post["id"])                 # 自己讚自己不通知

        mine = [row for row in self.events.list(account_id="owner")["events"]
                if row["type"] == "notice.created"]
        self.assertEqual([row["payload"]["kind"] for row in mine], ["reply", "like"])
        self.assertEqual(mine[0]["payload"]["reply_id"], reply["id"])
        self.assertEqual(mine[0]["payload"]["actor_display"], "アスター")
        self.assertEqual(mine[0]["payload"]["preview"], "きれいだね")
        self.assertEqual(mine[1]["payload"]["post_id"], post["id"])
        others = [row["type"] for row in self.events.list(account_id="guest-a")["events"]]
        self.assertNotIn("notice.created", others, "別人讀不到這條通知")

    def test_entity_post_author_gets_no_notice(self):
        post = self.service.post(self.aster, "観測ログ")["post"]
        self.service.reply(self.owner, post["id"], "おつかれ")
        self.service.like(self.owner, post["id"])
        types = [row["type"] for row in self.events.list()["events"]]
        self.assertNotIn("notice.created", types, "角色帖主照舊走 Agent 叫醒")

    def test_bad_cursor_is_explicit_and_empty_page_keeps_cursor(self):
        with self.assertRaisesRegex(ValueError, "bad_cursor"):
            self.events.list(after="not-a-number")
        with self.assertRaisesRegex(ValueError, "bad_cursor"):
            self.events.list(after=-1)
        empty = self.events.list(after=42)
        self.assertEqual(empty, {"events": [], "next_cursor": "42", "has_more": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
