#!/usr/bin/env python3
"""路由与分流（架构骨架验收）"""

from __future__ import annotations

from arch_common import *  # noqa: F401,F403


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.threads = ThreadStore()
        self.entities = EntityRegistry()
        self.entities.register(CharacterEntity("mira", default=True))
        self.entities.register(CharacterEntity("aster"))
        self.router = Router(self.threads, entities=self.entities,
                             pipelines=frozenset(BLUEPRINTS))
        self.now = 1000.0

    def test_first_text_creates_thread_and_chat_pipeline(self):
        decision = self.router.route(make_event(at=self.now), now=self.now)
        self.assertTrue(decision.created)
        self.assertEqual(decision.pipeline, "chat.text")
        self.assertEqual(decision.mode, DispatchMode.ATTACH)
        self.assertEqual(decision.scope.bank(), "blueagent-mira")

    def test_cross_source_continuity_same_entity_scene(self):
        first = self.router.route(make_event(source="phone", at=self.now), now=self.now)
        second = self.router.route(make_event(source="discord", at=self.now + 30), now=self.now + 30)
        self.assertEqual(first.thread_id, second.thread_id)
        self.assertFalse(second.created)
        self.assertIn("continuity", second.reason)
        self.assertEqual(set(self.threads.get(first.thread_id).sources), {"phone", "discord"})

    def test_entity_and_scene_split_threads(self):
        base = self.router.route(make_event(at=self.now), now=self.now)
        other_entity = self.router.route(make_event(entity="aster", at=self.now), now=self.now)
        other_scene = self.router.route(make_event(scene="trpg:1", at=self.now), now=self.now)
        self.assertNotEqual(base.thread_id, other_entity.thread_id)
        self.assertNotEqual(base.thread_id, other_scene.thread_id)

    def test_unknown_entity_falls_back_to_default_with_note(self):
        decision = self.router.route(make_event(entity="ghost", at=self.now), now=self.now)
        self.assertEqual(decision.entity, "mira")
        self.assertIn("entity-unknown(ghost)", decision.reason)

    def test_two_principals_get_separate_threads_and_private_scopes(self):
        owner = self.router.route(make_event(principal="owner", at=self.now), now=self.now)
        alice = self.router.route(make_event(principal="alice", at=self.now), now=self.now)
        self.assertNotEqual(owner.thread_id, alice.thread_id)
        self.assertEqual(alice.principal, "alice")
        self.assertEqual(alice.scope.private_bank(), "blueagent-mira-u-alice")
        self.assertIn("who=alice", alice.reason)

    def test_group_scope_shares_one_thread_and_tracks_participants(self):
        first = self.router.route(make_event(principal="owner", scope="group:discord-1",
                                             at=self.now), now=self.now)
        second = self.router.route(make_event(principal="alice", scope="group:discord-1",
                                              at=self.now + 5), now=self.now + 5)
        self.assertEqual(first.thread_id, second.thread_id)
        thread = self.threads.get(first.thread_id)
        self.assertEqual(thread.participants, {"owner", "alice"})
        # 同一條群線，但每個人的私事仍是各自的池
        self.assertNotEqual(first.scope.private_bank(), second.scope.private_bank())
        self.assertIn("who=alice@group-discord-1", second.reason)

    def test_group_side_conversation_is_observed(self):
        decision = self.router.route(make_event(
            principal="alice", scope="group:discord-1", addressed_to="bob",
            text="bob 你昨天说的那个", at=self.now), now=self.now)
        self.assertEqual(decision.engage, Engagement.OBSERVE)

    def test_group_mention_replies(self):
        decision = self.router.route(make_event(
            principal="alice", scope="group:discord-1", addressed_to="entity",
            text="mira 你觉得呢", at=self.now), now=self.now)
        self.assertEqual(decision.engage, Engagement.REPLY)

    def test_group_wake_word_replies_without_mention(self):
        decision = self.router.route(make_event(
            principal="alice", scope="group:discord-1",
            text="mira、你怎么看？", at=self.now), now=self.now)
        self.assertEqual(decision.engage, Engagement.REPLY)

    def test_group_room_message_defaults_to_observe(self):
        decision = self.router.route(make_event(
            principal="alice", scope="group:discord-1", addressed_to="group",
            text="大家晚上好", at=self.now), now=self.now)
        self.assertEqual(decision.engage, Engagement.OBSERVE)

    def test_ignore_target_is_dropped(self):
        decision = self.router.route(make_event(addressed_to="ignore", at=self.now),
                                     now=self.now)
        self.assertEqual(decision.engage, Engagement.IGNORE)

    def test_direct_message_always_replies(self):
        decision = self.router.route(make_event(principal="alice", at=self.now), now=self.now)
        self.assertEqual(decision.engage, Engagement.REPLY)

    def test_modes_by_source_class(self):
        decision = self.router.route(make_event(at=self.now), now=self.now)
        thread = self.threads.get(decision.thread_id)
        thread.begin_turn("t_busy", self.now)
        self.assertEqual(self.router.route(make_event(at=self.now + 1), now=self.now + 1).mode,
                         DispatchMode.QUEUE)
        self.assertEqual(self.router.route(
            make_event(cls=SourceClass.REALTIME, kind=InputKind.AUDIO, at=self.now + 1),
            now=self.now + 1).mode, DispatchMode.INTERRUPT)
        # 環境源只在**同一條線**上才被丟棄（不同 purpose 本來就是另一條線）。
        self.assertEqual(rules.choose_mode(
            make_event(cls=SourceClass.AMBIENT, purpose="ambient"), thread), DispatchMode.DROP)

    def test_pipeline_selection(self):
        self.assertEqual(self.router.route(make_event(kind=InputKind.IMAGE, at=self.now),
                                           now=self.now).pipeline, "media.see")
        self.assertEqual(self.router.route(
            make_event(cls=SourceClass.REALTIME, kind=InputKind.AUDIO, at=self.now),
            now=self.now).pipeline, "voice.realtime")
        self.assertEqual(self.router.route(make_event(purpose="trpg", at=self.now),
                                           now=self.now).pipeline, "trpg.turn")
        self.assertEqual(self.router.route(make_event(pipeline="chat.work", at=self.now),
                                           now=self.now).pipeline, "chat.work")

    def test_unknown_pipeline_hint_falls_back(self):
        decision = self.router.route(make_event(pipeline="nope", at=self.now), now=self.now)
        self.assertEqual(decision.pipeline, "chat.text")
        self.assertIn("unknown(nope)", decision.reason)

    def test_idle_seconds_reported_before_touch(self):
        self.router.route(make_event(at=self.now), now=self.now)
        later = self.router.route(make_event(at=self.now + 300), now=self.now + 300)
        self.assertAlmostEqual(later.idle_s, 300.0, places=3)


# ── E/F/G 併發、仲裁、取消 ────────────────────────────────────────────────────────


if __name__ == "__main__":
    unittest.main(verbosity=2)
