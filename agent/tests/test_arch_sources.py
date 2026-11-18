#!/usr/bin/env python3
"""源与注册表（架构骨架验收）"""

from __future__ import annotations

from arch_common import *  # noqa: F401,F403


class TestSources(unittest.TestCase):
    def test_base_source_normalizes_soul_to_entity(self):
        source = CollectSource("phone", entity="mira",
                               caps=SourceCaps(text_in=True, audio_in=False, image_in=True))
        event = source.normalize({"text": "hello", "soul": "aster", "at": 42.0,
                                  "trace": "tr-1", "idempotency_key": "msg-9"})
        self.assertEqual(event.trace_id, "tr-1")
        self.assertEqual(event.idempotency_key, "msg-9")
        self.assertEqual(event.entity, "aster")
        self.assertEqual(event.kind, InputKind.TEXT)
        self.assertEqual(event.source_class, SourceClass.INTERACTIVE)
        self.assertEqual(event.at, 42.0)
        self.assertTrue(source.caps.supports(InputKind.IMAGE))
        self.assertFalse(source.caps.supports(InputKind.AUDIO))

    def test_source_resolves_principal_from_trusted_meta(self):
        registry = PrincipalRegistry()
        registry.register(Principal("alice", "Alice"))
        registry.bind("discord", "u-123", "alice")
        source = CollectSource(
            "discord",
            resolver=lambda meta: registry.resolve(source="discord",
                                                   external_id=meta.get("user", "")))
        event = source.normalize({"text": "hi"}, meta={"user": "u-123"})
        self.assertEqual(event.principal, "alice")
        # payload 自報無效：這是最容易被偽造的地方
        spoofed = source.normalize({"text": "hi", "principal": "owner"}, meta={"user": "u-123"})
        self.assertEqual(spoofed.principal, "alice")

    def test_source_maps_mention_to_addressed_entity(self):
        source = CollectSource("discord", conversation=ConversationMode.GROUP,
                               scene="discord:1")
        event = source.normalize({"text": "hi", "mention_entity": True},
                                 meta={"principal": "alice"})
        self.assertEqual(event.addressed_to, "entity")

    def test_group_source_marks_group_scope(self):
        source = CollectSource("discord", conversation=ConversationMode.GROUP,
                               scene="discord:1")
        event = source.normalize({"text": "hi"}, meta={"principal": "alice"})
        self.assertEqual(event.principal, "alice")
        self.assertEqual(event.scope, "group:discord:1")

# ── 實體狀態：在場／注意力 ────────────────────────────────────────────────────────



class TestIngressRuntime(unittest.TestCase):
    def _runtime(self, *, stages, policy: SegmentPolicy | None = None):
        threads = ThreadStore()
        entities = EntityRegistry()
        entities.register(CharacterEntity("mira", default=True))
        pipelines = PipelineRegistry()
        for pipeline_id in BLUEPRINTS:
            pipelines.register(Pipeline(id=pipeline_id, stages=tuple(stages)))
        scheduler = Scheduler(SchedulerConfig(realtime_slots=2, interactive_slots=2, ambient_slots=1))
        archive = EpisodeArchive(policy=policy or SegmentPolicy())
        store = EntityStore()
        router = Router(threads, entities=entities, pipelines=pipelines.ids())
        runtime = IngressRuntime(router=router, threads=threads, pipelines=pipelines,
                                 scheduler=scheduler, episodes=archive, entities=store,
                                 ports=NullPorts())
        return runtime, threads, store, archive, scheduler

    def test_handle_records_turns_and_updates_attention(self):
        calls: list = []
        runtime, threads, entities, _archive, scheduler = self._runtime(
            stages=[SpyStage("brain", calls, record="answer", emit={"kind": "done",
                                                                   "payload": {"text": "answer"}})])
        source = CollectSource("phone")
        try:
            result = runtime.handle(source.normalize({"text": "hello", "at": 1000.0}), source=source)
            self.assertIsNotNone(result.job)
            self.assertTrue(result.job.wait(2.0))
            self.assertTrue(scheduler.drain(2.0))
            thread = threads.get(result.thread_id)
            self.assertEqual([t.role for t in thread.transcript], ["user", "assistant"])
            self.assertEqual([f.kind for f in source.frames], ["done"])
            self.assertEqual(entities.attention("mira"), thread.id)
        finally:
            scheduler.shutdown()

    def test_second_input_queues_until_first_turn_ends(self):
        calls: list = []
        entered = threading.Event()
        gate = threading.Event()
        runtime, threads, _entities, _archive, scheduler = self._runtime(
            stages=[SpyStage("brain", calls, gate=gate, entered=entered, record="answer")])
        source = CollectSource("phone")
        try:
            first = runtime.handle(source.normalize({"text": "one", "at": 1000.0}), source=source)
            self.assertTrue(entered.wait(2.0))
            second = runtime.handle(source.normalize({"text": "two", "at": 1001.0}), source=source)
            self.assertEqual(second.decision.mode, DispatchMode.QUEUE)
            thread = threads.get(first.thread_id)
            self.assertEqual(thread.queued, 1)
            gate.set()
            self.assertTrue(first.job.wait(2.0) and second.job.wait(2.0))
            self.assertTrue(scheduler.drain(2.0))
            self.assertEqual(calls, ["brain", "brain"])
            self.assertEqual(thread.queued, 0)
        finally:
            gate.set()
            scheduler.shutdown()

    def test_realtime_interrupts_inflight_turn(self):
        entered = threading.Event()
        gate = threading.Event()
        calls: list = []
        runtime, _threads, _entities, _archive, scheduler = self._runtime(
            stages=[SpyStage("brain", calls, gate=gate, entered=entered, record="answer")])
        source = CollectSource("phone")
        voice = CollectSource("mic", SourceClass.REALTIME)
        try:
            first = runtime.handle(source.normalize({"text": "long", "at": 1000.0}), source=source)
            self.assertTrue(entered.wait(2.0))
            second = runtime.handle(
                voice.normalize({"text": "hi", "kind": "text", "at": 1001.0}), source=voice)
            self.assertEqual(second.decision.mode, DispatchMode.INTERRUPT)
            self.assertTrue(first.job.token.cancelled)
            gate.set()
            self.assertTrue(scheduler.drain(2.0))
        finally:
            gate.set()
            scheduler.shutdown()

    def test_auto_seal_on_turn_budget(self):
        calls: list = []
        runtime, threads, _entities, archive, scheduler = self._runtime(
            stages=[SpyStage("brain", calls, record="answer")],
            policy=SegmentPolicy(max_turns=1))
        source = CollectSource("phone")
        try:
            first = runtime.handle(source.normalize({"text": "one", "at": 1000.0}), source=source)
            self.assertTrue(first.job.wait(2.0))
            second = runtime.handle(source.normalize({"text": "two", "at": 1001.0}), source=source)
            self.assertIsNotNone(second.sealed)
            self.assertEqual(second.sealed.reason, CutReason.TURNS)
            self.assertEqual(len(archive.all()), 1)
            self.assertTrue(second.job.wait(2.0))
        finally:
            scheduler.shutdown()

    def test_two_principals_get_their_own_threads_and_frames(self):
        runtime, threads, entities, _archive, scheduler = self._runtime(
            stages=[SpyStage("brain", [], record="answer",
                             emit={"kind": "done", "payload": {"text": "answer"}})])
        a = CollectSource("phone-a", principal="owner")
        b = CollectSource("phone-b", principal="alice")
        try:
            ra = runtime.handle(a.normalize({"text": "hi", "at": 1000.0}), source=a)
            rb = runtime.handle(b.normalize({"text": "hi", "at": 1000.0}), source=b)
            self.assertNotEqual(ra.thread_id, rb.thread_id)
            self.assertTrue(ra.job.wait(2.0) and rb.job.wait(2.0))
            self.assertEqual(len(a.frames), 1)
            self.assertEqual(len(b.frames), 1)
            self.assertEqual(threads.get(ra.thread_id).transcript[0].speaker, "owner")
            self.assertEqual(threads.get(rb.thread_id).transcript[0].speaker, "alice")
            self.assertEqual(entities.attention_principal("mira"), "alice")
        finally:
            scheduler.shutdown()

    def test_group_side_conversation_is_recorded_but_silent(self):
        calls: list = []
        runtime, threads, entities, _archive, scheduler = self._runtime(
            stages=[SpyStage("brain", calls, record="answer",
                             emit={"kind": "done", "payload": {"text": "answer"}})])
        source = CollectSource("discord", conversation=ConversationMode.GROUP,
                               scene="discord:1", principal="alice")
        try:
            event = source.normalize({"text": "bob 你昨天说的那个", "addressed_to": "bob",
                                      "at": 1000.0})
            result = runtime.handle(event, source=source)
            self.assertTrue(result.observed)
            self.assertIsNone(result.job)
            self.assertEqual(calls, [])
            self.assertEqual(source.frames, [])
            thread = threads.get(result.thread_id)
            self.assertEqual(thread.transcript[0].speaker, "alice")
            self.assertEqual(thread.transcript[0].addressed_to, "bob")
            self.assertEqual(entities.attention("mira"), "")
        finally:
            scheduler.shutdown()

    def test_group_mention_wakes_her_and_moves_attention(self):
        calls: list = []
        runtime, threads, entities, _archive, scheduler = self._runtime(
            stages=[SpyStage("brain", calls, record="answer",
                             emit={"kind": "done", "payload": {"text": "answer"}})])
        source = CollectSource("discord", conversation=ConversationMode.GROUP,
                               scene="discord:1", principal="alice")
        try:
            event = source.normalize({"text": "mira 你觉得呢", "mention_entity": True,
                                      "at": 1000.0})
            result = runtime.handle(event, source=source)
            self.assertIsNotNone(result.job)
            self.assertTrue(result.job.wait(2.0))
            self.assertEqual(calls, ["brain"])
            self.assertEqual(len(source.frames), 1)
            self.assertEqual(source.frames[0].reply_to, "alice")
            self.assertEqual(entities.attention_principal("mira"), "alice")
        finally:
            scheduler.shutdown()

    def test_duplicate_event_is_dropped(self):
        runtime, _threads, _entities, _archive, scheduler = self._runtime(
            stages=[SpyStage("brain", [], record="answer")])
        source = CollectSource("phone")
        try:
            first = runtime.handle(source.normalize(
                {"text": "hi", "at": 1000.0, "idempotency_key": "m-1"}), source=source)
            self.assertTrue(first.job.wait(2.0))
            duplicate = runtime.handle(source.normalize(
                {"text": "hi", "at": 1001.0, "idempotency_key": "m-1"}), source=source)
            self.assertTrue(duplicate.duplicate)
            self.assertIsNone(duplicate.decision)
            self.assertIsNone(duplicate.job)
        finally:
            scheduler.shutdown()

    def test_same_key_from_another_source_is_not_duplicate(self):
        runtime, _threads, _entities, _archive, scheduler = self._runtime(
            stages=[SpyStage("brain", [], record="answer")])
        phone = CollectSource("phone")
        discord = CollectSource("discord")
        try:
            first = runtime.handle(phone.normalize(
                {"text": "hi", "at": 1000.0, "idempotency_key": "m-1"}), source=phone)
            self.assertTrue(first.job.wait(2.0))
            other = runtime.handle(discord.normalize(
                {"text": "hi", "at": 1001.0, "idempotency_key": "m-1"}), source=discord)
            self.assertFalse(other.duplicate)
            self.assertIsNotNone(other.job)
            self.assertTrue(other.job.wait(2.0))
        finally:
            scheduler.shutdown()

    def test_signal_only_updates_presence_without_turn(self):
        runtime, _threads, entities, _archive, scheduler = self._runtime(stages=[SpyStage("brain", [])])
        source = CollectSource("phone")
        try:
            event = source.normalize({"kind": "signal", "at": 1000.0})
            result = runtime.handle(event, source=source)
            self.assertIsNone(result.decision)
            self.assertIsNone(result.job)
            self.assertEqual(len(entities.presence("mira")), 1)
        finally:
            scheduler.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
