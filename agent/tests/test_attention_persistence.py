#!/usr/bin/env python3
"""M3：租约持久化、shadow、reaction 能力、指标。"""

from __future__ import annotations

import json
import pathlib
import tempfile
import time

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.attention import AttentionConfig, FocusLease
from agent.brain.domain.decision import Engagement
from agent.brain.domain.event import IngressEvent, InputKind, SourceClass
from agent.brain.domain.thread import Thread, ThreadKey, TurnRecord
from agent.brain.runtime.attention import AttentionGate
from agent.brain.runtime.attention_history import AttentionHistory
from agent.brain.runtime.lease_store import FocusLeaseStore


def make_event(text: str, *, reactions: bool = True, addressed_to: str = "") -> IngressEvent:
    return IngressEvent(principal="alice", entity="mira", source="discord:alice",
                        kind=InputKind.TEXT, at=time.time(), source_class=SourceClass.INTERACTIVE,
                        scope="group:discord-lobby", scene="discord-lobby",
                        addressed_to=addressed_to, reactions=reactions,
                        payload={"text": text})


def make_thread(*, last_speaker: bool = True) -> Thread:
    thread = Thread(key=ThreadKey("group:discord-lobby", "mira", "discord-lobby", "chat"),
                    id=f"th_test-{time.time_ns()}", lane=SourceClass.INTERACTIVE,
                    created_at=time.time(), last_active_at=time.time())
    if last_speaker:
        thread.record(TurnRecord("t_prev", "user", "上一句", "discord:alice",
                                 time.time(), speaker="alice"))
    return thread


class TestLeasePersistence(unittest.TestCase):
    def test_grant_consume_survives_reopen(self):
        path = pathlib.Path(tempfile.mkdtemp(prefix="ba-lease-")) / "leases.json"
        store = FocusLeaseStore(path)
        config = AttentionConfig(lease_turns=3)
        now = time.time()
        store.grant(entity="mira", thread_id="th_1", principal="alice",
                    group="group:1", now=now, config=config)
        again = FocusLeaseStore(path)
        lease = again.match("th_1", "alice", now + 1)
        self.assertIsNotNone(lease)
        again.consume(lease)
        third = FocusLeaseStore(path)
        self.assertEqual(third.match("th_1", "alice", now + 2).turns_left, 2)

    def test_expired_lease_is_pruned(self):
        path = pathlib.Path(tempfile.mkdtemp(prefix="ba-lease-")) / "leases.json"
        store = FocusLeaseStore(path)
        store.grant(entity="mira", thread_id="th_1", principal="alice",
                    group="group:1", now=100.0, config=AttentionConfig(lease_ttl_s=10))
        self.assertIsNone(store.match("th_1", "alice", 111.0))
        self.assertEqual(store.snapshot(111.0), [])


class TestAttentionHistory(unittest.TestCase):
    def test_history_aggregates_and_reloads(self):
        path = pathlib.Path(tempfile.mkdtemp(prefix="ba-history-")) / "attention.jsonl"
        history = AttentionHistory(path)
        now = time.time()
        history.record({"at": now, "actual": "yield", "proposed": "yield",
                        "scope": "group:1", "shadow": False})
        history.record({"at": now, "actual": "silent", "proposed": "react",
                        "scope": "group:1", "shadow": True})
        summary = history.summary()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["shadow"], 1)
        self.assertEqual(summary["by_action"]["yield"], 1)
        self.assertEqual(summary["groups"][0]["group"], "group:1")
        loaded = AttentionHistory(path).summary()
        self.assertEqual(loaded["total"], 2)
        self.assertEqual(loaded["shadow"], 1)


class TestShadowAndCapabilities(unittest.TestCase):
    def test_shadow_keeps_behavior_but_logs_proposal(self):
        log = pathlib.Path(tempfile.mkdtemp(prefix="ba-gate-")) / "attention.jsonl"
        gate = AttentionGate(AttentionConfig(shadow=True), log_path=log)
        decision = gate.decide(make_event("周末回老家吗？"), make_thread(),
                               Engagement.OBSERVE, recall_hits=1,
                               reactions=True, now=time.time())
        self.assertIn(decision.action, {Engagement.OBSERVE, Engagement.REPLY})
        self.assertTrue(decision.reason.startswith("shadow:"))
        row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["shadow"], True)
        self.assertEqual(row["proposed"], "yield")

    def test_reaction_requires_source_capability(self):
        memo = "大家注意下，明天下午三点跟客户对齐"
        gate = AttentionGate(AttentionConfig(react_threshold=0.25))
        without = gate.decide(make_event(memo, reactions=False), make_thread(),
                              Engagement.OBSERVE, now=time.time())
        self.assertNotEqual(without.action, Engagement.REACT)
        gate2 = AttentionGate(AttentionConfig(react_threshold=0.25))
        with_cap = gate2.decide(make_event(memo, reactions=True), make_thread(),
                                Engagement.OBSERVE, now=time.time())
        self.assertEqual(with_cap.action, Engagement.REACT)

    def test_voice_group_uses_short_yield_window(self):
        event = IngressEvent(principal="alice", entity="mira", source="mic:alice",
                             kind=InputKind.AUDIO, at=time.time(),
                             source_class=SourceClass.REALTIME,
                             scope="group:voice-lobby", scene="voice-lobby",
                             reactions=False,
                             payload={"text": "有人知道周末回老家吗？"})
        gate = AttentionGate(AttentionConfig(voice_yield_ms=1200))
        decision = gate.decide(event, make_thread(), Engagement.OBSERVE,
                               recall_hits=1, now=time.time())
        self.assertEqual(decision.action, Engagement.YIELD)
        self.assertEqual(decision.yield_ms, 1200)

    def test_metrics_count_and_recent(self):
        gate = AttentionGate(AttentionConfig())
        gate.decide(make_event("有人知道周末回老家吗？"), make_thread(),
                    Engagement.OBSERVE, recall_hits=1, now=time.time())
        snapshot = gate.snapshot("mira", "th_1", time.time())
        self.assertGreaterEqual(snapshot["metrics"]["total"], 1)
        self.assertTrue(snapshot["recent"])

    def test_pending_yield_survives_restart(self):
        root = tempfile.mkdtemp(prefix="ba-pending-")
        demo = build(brain_delay=0, data_dir=root, step_duration=0.02,
                     life_pause=0.005,
                     attention_config=AttentionConfig(yield_ms=300))
        demo.principals.register(Principal("owner", "主人", owner=True), replace=True)
        state = AppState(demo)
        state.send("周末回老家吗？", entity="mira", principal="alice",
                   group=True, participants=["owner", "alice"])
        self.assertTrue(state.snapshot()["pending"])
        state.shutdown()

        again = build(brain_delay=0, data_dir=root, step_duration=0.02,
                      life_pause=0.005,
                      attention_config=AttentionConfig(yield_ms=300))
        again.principals.register(Principal("owner", "主人", owner=True), replace=True)
        state2 = AppState(again)
        try:
            self.assertTrue(wait_for(lambda: not state2.snapshot()["pending"], 2.0))
            thread = next(t for t in state2.snapshot()["threads"]
                          if t["scope"] == "group-discord-lobby")
            self.assertTrue(any(message["role"] == "assistant"
                                for message in thread["messages"]))
        finally:
            state2.shutdown()

    def test_demo_lease_persists_across_new_state(self):
        root = tempfile.mkdtemp(prefix="ba-demo-lease-")
        demo = build(brain_delay=0, data_dir=root, step_duration=0.02, life_pause=0.005)
        demo.principals.register(Principal("owner", "主人", owner=True), replace=True)
        state = AppState(demo)
        state.send("ミラ，你觉得呢？", entity="mira", principal="alice",
                   group=True, mention=True, participants=["owner", "alice"])
        thread = next(t for t in state.snapshot()["threads"]
                      if t["scope"] == "group-discord-lobby")
        demo.shutdown_lives()

        again = build(brain_delay=0, data_dir=root, step_duration=0.02, life_pause=0.005)
        again.principals.register(Principal("owner", "主人", owner=True), replace=True)
        self.assertIsNotNone(again.attention.leases.match(thread["id"], "alice", time.time()))
        again.shutdown_lives()


if __name__ == "__main__":
    unittest.main(verbosity=2)
