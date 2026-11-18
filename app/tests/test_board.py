#!/usr/bin/env python3
"""App Server 公共区：迁移快照、原文／索引、权限、分页与互动判据。"""

from __future__ import annotations

import pathlib
import tempfile
import time
import unittest

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.social.models import Actor, BoardError, Post
from app.server.social.service import BoardService


class BoardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-board-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "logs").mkdir()
        self.now = 1000.0
        self.feedback = []
        migrated = [
            Post(id="pst_act_aster", at=110.0, author_kind="entity", author_id="aster",
                 display="アスター", body="最终结论", origin="projection", kind="work",
                 source_ref="activity:aster:a1"),
            Post(id="pst_work_nori", at=140.0, author_kind="entity", author_id="nori",
                 display="ノリ", body="作品收尾", origin="projection", kind="work",
                 source_ref="work:nori:final"),
        ]
        self.service = BoardService(
            self.root, clock=lambda: self.now,
            feedback=lambda entity, text: self.feedback.append((entity, text)),
            names={"owner": "主人", "aster": "アスター", "nori": "ノリ"},
            projection=lambda _root: migrated)
        self.addCleanup(self.service.close)
        self.owner = Actor("owner", "owner", "主人")
        self.guest = Actor("guest:1", "guest", "客人一", frozenset({"aster"}))
        self.other = Actor("guest:2", "guest", "客人二")

    def test_migrated_projection_remains_read_only_snapshot(self):
        feed = self.service.feed(self.owner)
        self.assertEqual(len(feed["posts"]), 2, feed)
        by_author = {post["author_id"]: post for post in feed["posts"]}
        self.assertEqual(by_author["aster"]["body"], "最终结论")
        self.assertEqual(by_author["aster"]["kind"], "work")
        self.assertEqual(by_author["nori"]["body"], "作品收尾")
        self.assertTrue(all(post["origin"] == "projection" for post in feed["posts"]))

    def test_guest_bound_entity_only_loses_other_projections_not_public_people(self):
        self.service.post(self.owner, "主人的公开投稿")
        feed = self.service.feed(self.guest)
        projections = [post for post in feed["posts"] if post["origin"] == "projection"]
        self.assertEqual({post["author_id"] for post in projections}, {"aster"})
        self.assertIn("主人的公开投稿", {post["body"] for post in feed["posts"]})

    def test_manual_post_writes_jsonl_and_db_without_touching_activity(self):
        activity = {path: path.stat().st_mtime_ns for path in (self.root / "logs").glob("*.jsonl")}
        result = self.service.post(self.guest, "客人正文", ["m1", "m1", "m2"])
        post = result["post"]
        self.assertEqual(post["author_kind"], "guest")
        self.assertEqual(post["author_id"], "guest:1")
        self.assertEqual(post["media"], ["m1", "m2"])
        month = time.strftime("%Y-%m.jsonl", time.localtime(self.now))
        original = (self.root / "board" / month).read_text(encoding="utf-8")
        self.assertIn("客人正文", original)
        self.assertEqual(self.service.store.get(post["id"]).body, "客人正文")
        self.assertEqual(activity, {path: path.stat().st_mtime_ns
                                    for path in (self.root / "logs").glob("*.jsonl")})

    def test_input_limits_and_bad_media(self):
        cases = [("", [], "empty_post"), ("x" * 2001, [], "body_too_long"),
                 ("x", ["m1", "m2", "m3", "m4", "m5"], "too_many_media"),
                 ("x", ["../../secret"], "bad_media")]
        for body, media, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(BoardError) as caught:
                    self.service.post(self.owner, body, media)
                self.assertEqual(caught.exception.code, code)

    def test_pagination_is_stable_even_with_identical_timestamps(self):
        expected = []
        for index in range(7):
            post = Post(id=f"pst_manual_{index}", at=2000.0, author_kind="owner",
                        author_id="owner", display="主人", body=f"帖 {index}")
            self.service.store.create_manual(post)
            expected.append(post.id)
        expected.sort(reverse=True)
        found, cursor = [], ""
        pages = 0
        while True:
            result = self.service.feed(self.owner, before=cursor, limit=3, since=1500)
            found.extend(post["id"] for post in result["posts"])
            cursor = result["next_cursor"]
            pages += 1
            if not cursor:
                break
        self.assertEqual(pages, 3)
        self.assertEqual(found, expected)
        self.assertEqual(len(found), len(set(found)))
        with self.assertRaises(ValueError):
            self.service.feed(self.owner, before="not-a-cursor")

    def test_reply_is_flat_and_updates_counter(self):
        root = self.service.post(self.owner, "根帖")["post"]
        reply = self.service.reply(self.guest, root["id"], "公开回复")["post"]
        self.assertEqual(reply["parent_id"], root["id"])
        with self.assertRaises(BoardError) as caught:
            self.service.reply(self.other, reply["id"], "二层不允许")
        self.assertEqual(caught.exception.code, "nested_reply_not_allowed")
        detail = self.service.get(self.owner, root["id"])
        self.assertEqual(detail["post"]["replies"], 1)
        self.assertEqual([row["body"] for row in detail["replies"]], ["公开回复"])
        feed = self.service.feed(self.owner)
        self.assertNotIn("公开回复", {post["body"] for post in feed["posts"]})

    def test_like_is_idempotent_and_feedback_fires_once(self):
        projection = next(post for post in self.service.feed(self.owner)["posts"]
                          if post["author_id"] == "aster")
        first = self.service.like(self.guest, projection["id"])
        second = self.service.like(self.guest, projection["id"])
        self.assertTrue(first["added"])
        self.assertFalse(second["added"])
        self.assertEqual(second["post"]["likes"], 1)
        self.assertTrue(second["post"]["liked"])
        self.assertEqual(len(self.feedback), 1)
        self.assertEqual(self.feedback[0][0], projection["author_id"])

    def test_delete_permissions_include_author_and_owner(self):
        own = self.service.post(self.guest, "自己删除")["post"]
        other = self.service.post(self.other, "别人删除")["post"]
        with self.assertRaises(BoardError) as caught:
            self.service.delete(self.guest, other["id"])
        self.assertEqual(caught.exception.code, "denied")
        self.service.delete(self.guest, own["id"])
        self.service.delete(self.owner, other["id"])
        for post_id in (own["id"], other["id"]):
            with self.assertRaises(BoardError) as caught:
                self.service.get(self.owner, post_id)
            self.assertEqual(caught.exception.code, "not_found")

    def test_deleted_projection_cannot_be_read_or_liked_by_id(self):
        projection = next(post for post in self.service.feed(self.owner)["posts"]
                          if post["author_id"] == "aster")
        self.service.delete(self.owner, projection["id"])
        for operation in (lambda: self.service.get(self.owner, projection["id"]),
                          lambda: self.service.like(self.owner, projection["id"])):
            with self.assertRaises(BoardError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "not_found")
        self.assertNotIn(projection["id"],
                         {post["id"] for post in self.service.feed(self.owner)["posts"]})

    def test_sixth_manual_post_in_one_minute_is_429(self):
        for index in range(5):
            self.service.post(self.guest, f"第 {index} 帖")
        with self.assertRaises(BoardError) as caught:
            self.service.post(self.guest, "第六帖")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.code, "rate_limited")

    def test_disabled_board_keeps_only_projection(self):
        self.service.post(self.owner, "手动隐藏")
        disabled = BoardService(self.root, enabled=False, projection=self.service.projection)
        self.addCleanup(disabled.close)
        feed = disabled.feed(self.owner)
        self.assertTrue(feed["posts"])
        self.assertTrue(all(post["origin"] == "projection" for post in feed["posts"]))
        with self.assertRaises(BoardError) as caught:
            disabled.post(self.owner, "不能写")
        self.assertEqual(caught.exception.status, 403)

    def test_media_must_be_owned_and_shared_only_by_a_visible_live_post(self):
        self.service.media_owner = lambda media_id: {"mine": "guest:1", "other": "guest:2"}.get(media_id)
        for media_id in ("other", "missing"):
            with self.subTest(media_id=media_id), self.assertRaises(BoardError) as caught:
                self.service.post(self.guest, "不能拿别人的图", [media_id])
            self.assertEqual(caught.exception.code, "unknown_media")
        self.assertFalse(self.service.can_read_media(self.other, "mine", "guest:1"))
        created = self.service.post(self.guest, "公开图", ["mine"])["post"]
        self.assertTrue(self.service.can_read_media(self.other, "mine", "guest:1"))
        self.service.delete(self.guest, created["id"])
        self.assertFalse(self.service.can_read_media(self.other, "mine", "guest:1"))
        self.assertTrue(self.service.can_read_media(self.guest, "mine", "guest:1"))

    def test_request_media_owner_override_does_not_mutate_shared_resolver(self):
        self.service.media_owner = lambda media_id: "guest:1"
        created = self.service.post(self.other, "请求级归属", ["mine"],
                                    media_owner=lambda media_id: "guest:2")
        self.assertEqual(created["post"]["media"], ["mine"])
        self.assertEqual(self.service.media_owner("mine"), "guest:1")
        with self.assertRaises(BoardError) as caught:
            self.service.post(self.other, "默认归属仍生效", ["mine"])
        self.assertEqual(caught.exception.code, "unknown_media")

    def test_feed_limit_60_stays_fast_on_local_files(self):
        started = time.perf_counter()
        result = self.service.feed(self.owner, limit=60)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertLessEqual(len(result["posts"]), 60)
        self.assertLess(elapsed_ms, 200.0)

    def test_entity_post_may_carry_occurred_at_within_the_backfill_window(self):
        """⑦ REVIEW 2026-11-07 #7：角色帖的时间用活动做完的时刻，不是发布时刻。"""
        self.now = 1_794_000_000.0            # 真实量级的时间戳（0 附近会被当成「没带」）
        entity = Actor("aster", "entity", "アスター")
        occurred = self.now - 3600.0
        created = self.service.post(entity, "観測の結論", [], "m1",
                                    source_ref="activity:aster:a9", occurred_at=occurred)
        self.assertEqual(created["post"]["at"], occurred)

    def test_occurred_at_is_rejected_for_humans_without_source_ref_and_out_of_window(self):
        self.now = 1_794_000_000.0
        entity = Actor("aster", "entity", "アスター")
        with self.assertRaises(BoardError) as human:
            self.service.post(self.owner, "人类的帖子", [], occurred_at=self.now - 60.0)
        self.assertEqual(human.exception.code, "occurred_at_not_allowed")
        with self.assertRaises(BoardError) as no_ref:
            self.service.post(entity, "没有来源", [], occurred_at=self.now - 60.0)
        self.assertEqual(no_ref.exception.code, "occurred_at_not_allowed")
        with self.assertRaises(BoardError) as future:
            self.service.post(entity, "未来", [], source_ref="activity:aster:f1",
                              occurred_at=self.now + 3600.0)
        self.assertEqual(future.exception.code, "occurred_at_in_future")
        with self.assertRaises(BoardError) as too_old:
            self.service.post(entity, "太旧", [], source_ref="activity:aster:o1",
                              occurred_at=self.now - 30 * 86400)
        self.assertEqual(too_old.exception.code, "occurred_at_too_old")

    def test_backfilled_posts_are_rate_limited_on_real_time_not_occurred_at(self):
        """REVIEW 2026-11-08：回填帖不能因为 `occurred_at` 在过去就永远 429，也不能绕过限流。"""
        self.now = 1_794_000_000.0
        entity = Actor("aster", "entity", "アスター")
        # 先在「两天前之后」堆满 5 条（以前回填帖的窗口落在过去，会把这些全数进去 ⇒ 永远 429）
        for i in range(5):
            self.service.post(entity, f"今日の帖 {i}", [], f"n{i}")
            self.now += 13.0
        self.now += 61.0                       # 真实时间已过一分钟 ⇒ 应当放行
        old = self.now - 2 * 86400
        created = self.service.post(entity, "二日前の結論", [], "b0",
                                    source_ref="activity:aster:old0", occurred_at=old)
        self.assertEqual(created["post"]["at"], old)
        # 同一真实分钟里再回填 4 条到上限，第 6 条要被挡——回填帖照样受限流
        for i in range(1, 5):
            self.service.post(entity, f"回填 {i}", [], f"b{i}",
                              source_ref=f"activity:aster:old{i}", occurred_at=old + i)
        with self.assertRaises(BoardError) as limited:
            self.service.post(entity, "回填 5", [], "b5",
                              source_ref="activity:aster:old5", occurred_at=old + 5)
        self.assertEqual(limited.exception.code, "rate_limited")


if __name__ == "__main__":
    unittest.main(verbosity=2)
