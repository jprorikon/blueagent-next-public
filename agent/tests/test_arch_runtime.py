#!/usr/bin/env python3
"""并发、调度、管線（架构骨架验收）"""

from __future__ import annotations

from arch_common import *  # noqa: F401,F403


class TestConcurrency(unittest.TestCase):
    def test_scheduler_runs_and_drains(self):
        scheduler = Scheduler(SchedulerConfig(realtime_slots=2, interactive_slots=2, ambient_slots=1))
        done: list = []
        try:
            jobs = [scheduler.submit(make_decision("th_a", lane=SourceClass.INTERACTIVE),
                                     lambda token, i=i: done.append(i)) for i in range(3)]
            self.assertTrue(all(job.wait(2.0) for job in jobs))
            self.assertTrue(scheduler.drain(2.0))
            self.assertEqual(sorted(done), [0, 1, 2])
        finally:
            scheduler.shutdown()

    def test_non_realtime_is_serial_per_thread(self):
        scheduler = Scheduler(SchedulerConfig(interactive_slots=2))
        gate = threading.Event()
        active = {"now": 0, "max": 0}
        lock = threading.Lock()

        def job(_token):
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            gate.wait(1.0)
            with lock:
                active["now"] -= 1

        try:
            first = scheduler.submit(make_decision("th_same"), job)
            second = scheduler.submit(make_decision("th_same"), job)
            time.sleep(0.05)
            self.assertFalse(second.done.is_set(), "同一條線不該並行")
            gate.set()
            self.assertTrue(first.wait(2.0) and second.wait(2.0))
            self.assertEqual(active["max"], 1)
        finally:
            scheduler.shutdown()

    def test_realtime_is_parallel_and_can_preempt(self):
        scheduler = Scheduler(SchedulerConfig(realtime_slots=2, interactive_slots=1))
        entered = threading.Event()
        gate = threading.Event()

        def blocking(token):
            entered.set()
            gate.wait(2.0)

        try:
            slow = scheduler.submit(make_decision("th_voice", lane=SourceClass.INTERACTIVE), blocking)
            self.assertTrue(entered.wait(2.0))
            scheduler.submit(make_decision("th_voice", lane=SourceClass.REALTIME,
                                           mode=DispatchMode.INTERRUPT), lambda token: None)
            self.assertTrue(slow.token.cancelled, "實時插話應取消舊回合")
            gate.set()
            self.assertTrue(scheduler.drain(2.0))
        finally:
            scheduler.shutdown()

    def test_realtime_jumps_ahead_of_queued_ambient(self):
        scheduler = Scheduler(SchedulerConfig(ambient_slots=1, realtime_slots=1, interactive_slots=1))
        started = threading.Event()
        release = threading.Event()
        order: list = []

        def blocker(token):
            started.set()
            release.wait(2.0)

        try:
            scheduler.submit(make_decision("th_amb", lane=SourceClass.AMBIENT, priority=20), blocker)
            self.assertTrue(started.wait(2.0))
            scheduler.submit(make_decision("th_amb2", lane=SourceClass.AMBIENT, priority=21),
                             lambda token: order.append("ambient"))
            rt = scheduler.submit(make_decision("th_rt", lane=SourceClass.REALTIME, priority=0),
                                  lambda token: order.append("realtime"))
            self.assertTrue(rt.wait(2.0))
            self.assertEqual(order, ["realtime"])
        finally:
            release.set()
            scheduler.shutdown()

    def test_floor_arbiter_one_mouth_per_entity(self):
        arbiter = FloorArbiter()
        self.assertTrue(arbiter.acquire("mira", "th_a", priority=10))
        self.assertFalse(arbiter.acquire("mira", "th_b", priority=10, timeout=0.05))
        self.assertTrue(arbiter.acquire("mira", "th_c", priority=0))   # 更實時 → 插話
        self.assertEqual(arbiter.holder("mira"), "th_c")
        self.assertFalse(arbiter.release("mira", "th_a"))
        self.assertTrue(arbiter.release("mira", "th_c"))
        self.assertTrue(arbiter.acquire("aster", "th_r", priority=10))      # 另一個實體不受影響
        arbiter.acquire("mira", "th_p", priority=0, principal="alice")
        self.assertEqual(arbiter.current("mira").principal, "alice")

    def test_cancel_token_is_cross_thread(self):
        token = CancelToken()
        self.assertFalse(token.cancelled)
        threading.Thread(target=token.cancel).start()
        time.sleep(0.02)
        self.assertTrue(token.cancelled)


# ── 管線與 epoch 守衛 ─────────────────────────────────────────────────────────────
class TestPipeline(unittest.TestCase):
    def _ctx(self, thread: Thread, calls: list, *, sink=None) -> tuple[TurnContext, list]:
        frames: list = []

        def capture(frame):
            frames.append(frame)
            if sink:
                sink.append(frame)

        epoch = thread.begin_turn("t_1", 1000.0)
        return TurnContext(thread=thread, turn_id="t_1", epoch=epoch, cancel=CancelToken(),
                           sink=capture, source="phone"), frames

    def test_stages_run_in_order_and_can_stop(self):
        calls: list = []
        pipeline = make_pipeline("chat.text", calls,
                                 SpyStage("a", calls), SpyStage("b", calls),
                                 SpyStage("c", calls, outcome=StageOutcome.STOP),
                                 SpyStage("d", calls))
        thread = make_thread()
        env = Envelope(make_event(), make_decision(thread.id), thread, "t_1", "hi")
        ctx, frames = self._ctx(thread, calls)
        pipeline.run(env, ctx)
        self.assertEqual(calls, ["a", "b", "c"])

    def test_emit_records_frames_and_assistant_turn(self):
        calls: list = []
        pipeline = make_pipeline("chat.text", calls,
                                 SpyStage("a", calls, record="answer",
                                          emit={"kind": "done", "payload": {"text": "answer"}}))
        thread = make_thread()
        env = Envelope(make_event(), make_decision(thread.id), thread, "t_1", "hi")
        ctx, frames = self._ctx(thread, calls)
        pipeline.run(env, ctx)
        self.assertEqual(frames[0].kind, "done")
        self.assertEqual(frames[0].thread_id, thread.id)
        self.assertEqual(frames[0].reply_to, "")
        self.assertEqual([t.role for t in thread.segment], ["assistant"])

    def test_epoch_guard_drops_stale_frames(self):
        thread = make_thread()
        ctx, frames = self._ctx(thread, [])
        thread.begin_turn("t_2", 1001.0)               # 新回合開始 → 舊 ctx 過期
        self.assertTrue(ctx.stale())
        self.assertFalse(ctx.emit("delta", {"text": "late"}))
        self.assertEqual(frames, [])

    def test_cancelled_pipeline_stops_before_first_stage(self):
        calls: list = []
        thread = make_thread()
        pipeline = make_pipeline("chat.text", calls, SpyStage("a", calls))
        env = Envelope(make_event(), make_decision(thread.id), thread, "t_1", "hi")
        ctx, frames = self._ctx(thread, calls)
        ctx.cancel.cancel()
        pipeline.run(env, ctx)
        self.assertEqual(calls, [])
        self.assertEqual(frames, [])

    def test_blueprint_build_rejects_missing_stage(self):
        with self.assertRaises(KeyError):
            build("chat.text", {})
        stages = {name: SpyStage(name, []) for name in BLUEPRINTS["chat.text"]}
        pipeline = build("chat.text", stages)
        self.assertEqual([s.name for s in pipeline.stages], list(BLUEPRINTS["chat.text"]))

    def test_context_budget_allocates_exact_total(self):
        alloc = ContextBudget(total=1000).alloc()
        self.assertEqual(set(alloc), {"system", "active", "recent", "recall"})
        self.assertEqual(sum(alloc.values()), 1000)
        self.assertEqual(DEFAULT_CONTEXT_BUDGET.total, 16000)
        self.assertEqual(VOICE_CONTEXT_BUDGET.total, 6000)
        self.assertEqual(sum(VOICE_CONTEXT_BUDGET.alloc().values()), 6000)

    def test_default_pipeline_table(self):
        self.assertEqual(default_pipeline(SourceClass.REALTIME, InputKind.AUDIO, "chat"), "voice.realtime")
        self.assertEqual(default_pipeline(SourceClass.INTERACTIVE, InputKind.TEXT, "chat"), "chat.text")
        self.assertEqual(default_pipeline(SourceClass.INTERACTIVE, InputKind.IMAGE, "chat"), "media.see")
        self.assertEqual(default_pipeline(SourceClass.AMBIENT, InputKind.TEXT, "ambient"), "ambient.note")
        self.assertEqual(default_pipeline(SourceClass.INTERACTIVE, InputKind.TEXT, "game"), "game.talk")


# ── 源模組化 ──────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    unittest.main(verbosity=2)
