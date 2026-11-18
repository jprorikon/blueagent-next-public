#!/usr/bin/env python3
"""C-02／C-03：持久投影快照、媒体 ACL 与 Agent 显式发布幂等。"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.social.models import Actor, BoardError, Post
from app.server.social.service import BoardService


class StoredProjectionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-board-snapshot-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.service = BoardService(self.root, clock=lambda: 1000.0)
        self.addCleanup(self.service.close)
        self.owner = Actor("acct-owner", "owner", "主人")
        self.aster = Actor("aster", "entity", "アスター")
        self.guest = Actor("acct-guest", "guest", "客人", frozenset({"aster"}))

    def projection(self, post_id="pst_act_one", body="公开快照", media=None):
        return Post(id=post_id, at=100.0, author_kind="entity", author_id="aster",
                    display="アスター", body=body, origin="projection", kind="mutter",
                    media=list(media or []), source_ref="activity:aster:one")

    def test_stored_projection_needs_no_agent_files_and_preserves_acl(self):
        projected = self.projection(media=["m0123456789abcdef"])
        self.service.store.upsert_projection(projected)
        feed = self.service.feed(self.guest)
        self.assertEqual([row["id"] for row in feed["posts"]], [projected.id])
        self.assertEqual(self.service.get(self.guest, projected.id)["post"]["body"], "公开快照")
        self.assertTrue(self.service.can_read_media(self.guest, "m0123456789abcdef"))
        denied = Actor("acct-other", "guest", "别人", frozenset({"nori"}))
        self.assertEqual(self.service.feed(denied)["posts"], [])
        self.assertFalse(self.service.can_read_media(denied, "m0123456789abcdef"))

        self.service.delete(self.owner, projected.id)
        self.assertEqual(self.service.feed(self.owner)["posts"], [])
        self.assertFalse(self.service.can_read_media(self.owner, "m0123456789abcdef"))
        self.service.store.upsert_projection(self.projection())
        self.assertEqual(self.service.feed(self.owner)["posts"], [], "刷新快照不得撤销软删")

    def test_explicit_source_ref_is_idempotent_and_fail_closed(self):
        first = self.service.post(self.aster, "只发布一次", source_ref="activity:aster:publish")
        replay = self.service.post(self.aster, "只发布一次", source_ref="activity:aster:publish")
        self.assertEqual(replay["post"]["id"], first["post"]["id"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(self.service.store.manual_posts()), 1)

        with self.assertRaises(BoardError) as caught:
            self.service.post(self.aster, "改掉原文", source_ref="activity:aster:publish")
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (409, "source_ref_conflict"))
        with self.assertRaises(BoardError) as caught:
            self.service.post(self.owner, "只发布一次", source_ref="activity:aster:publish")
        self.assertEqual(caught.exception.code, "source_ref_conflict")
        with self.assertRaises(BoardError) as caught:
            self.service.post(self.aster, "坏引用", source_ref="没有命名空间")
        self.assertEqual(caught.exception.code, "bad_source_ref")

        self.service.delete(self.aster, first["post"]["id"])
        with self.assertRaises(BoardError) as caught:
            self.service.post(self.aster, "只发布一次", source_ref="activity:aster:publish")
        self.assertEqual(caught.exception.code, "source_ref_deleted")

    def test_source_ref_participates_in_mutation_identity(self):
        first = self.service.post(self.aster, "正文", client_mutation_id="cm-publish",
                                  source_ref="work:aster:final")
        replay = self.service.post(self.aster, "正文", client_mutation_id="cm-publish",
                                   source_ref="work:aster:final")
        self.assertEqual(replay, first)
        with self.assertRaises(BoardError) as caught:
            self.service.post(self.aster, "正文", client_mutation_id="cm-publish",
                              source_ref="work:aster:other")
        self.assertEqual(caught.exception.code, "mutation_conflict")


if __name__ == "__main__":
    unittest.main(verbosity=2)
