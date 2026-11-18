#!/usr/bin/env python3
"""S2 公共区 mutation：断线重放返回首次响应，冲突与原子失败不能造第二帖。"""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.events import EventLog
from app.server.social.models import Actor, BoardError
from app.server.social.service import BoardService


class AppMutationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-app-mutations-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.now = 1000.0
        self.service = BoardService(self.root, clock=lambda: self.now)
        self.addCleanup(self.service.close)
        self.owner = Actor("owner", "owner", "主人")

    def test_post_replay_returns_same_response_without_second_post_or_event(self):
        first = self.service.post(self.owner, "只写一次", client_mutation_id="cm_post")
        second = self.service.post(self.owner, "只写一次", client_mutation_id="cm_post")
        self.assertEqual(second, first)
        self.assertEqual(len(self.service.store.manual_posts()), 1)
        events = EventLog(self.service.store.db_path)
        self.addCleanup(events.close)
        self.assertEqual([event["type"] for event in events.list()["events"]], ["post.created"])

    def test_same_key_with_different_payload_is_conflict(self):
        self.service.post(self.owner, "原文", client_mutation_id="cm_conflict")
        with self.assertRaises(BoardError) as caught:
            self.service.post(self.owner, "改掉内容", client_mutation_id="cm_conflict")
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "mutation_conflict"))
        self.assertEqual(len(self.service.store.manual_posts()), 1)

    def test_reply_replay_survives_restart_and_does_not_count_twice(self):
        root = self.service.post(self.owner, "根帖")["post"]
        first = self.service.reply(self.owner, root["id"], "一次回复", client_mutation_id="cm_reply")
        self.service.close()
        self.service = BoardService(self.root, clock=lambda: self.now)
        second = self.service.reply(self.owner, root["id"], "一次回复", client_mutation_id="cm_reply")
        self.assertEqual(second, first)
        self.assertEqual(self.service.get(self.owner, root["id"])["post"]["replies"], 1)

    def test_cached_response_bypasses_rate_limit_on_transport_retry(self):
        first = self.service.post(self.owner, "第一帖", client_mutation_id="cm_first")
        for index in range(4):
            self.service.post(self.owner, f"后续 {index}")
        replay = self.service.post(self.owner, "第一帖", client_mutation_id="cm_first")
        self.assertEqual(replay, first)
        with self.assertRaises(BoardError) as caught:
            self.service.post(self.owner, "真正第六帖", client_mutation_id="cm_sixth")
        self.assertEqual(caught.exception.code, "rate_limited")

    def test_event_failure_rolls_back_mutation_evidence(self):
        with mock.patch("app.server.social.store.append_event", side_effect=RuntimeError("event failed")):
            with self.assertRaisesRegex(RuntimeError, "event failed"):
                self.service.post(self.owner, "原子", client_mutation_id="cm_atomic")
        self.assertEqual(self.service.store.manual_posts(), [])
        created = self.service.post(self.owner, "原子", client_mutation_id="cm_atomic")
        self.assertEqual(created["post"]["body"], "原子")
        self.assertEqual(len(self.service.store.manual_posts()), 1)

    def test_bad_mutation_id_is_rejected(self):
        with self.assertRaises(BoardError) as caught:
            self.service.post(self.owner, "坏键", client_mutation_id="has spaces")
        self.assertEqual(caught.exception.code, "bad_mutation_id")


if __name__ == "__main__":
    unittest.main(verbosity=2)
