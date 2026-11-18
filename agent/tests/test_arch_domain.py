#!/usr/bin/env python3
"""实体优先 / 自动切片 / 关系（架构骨架验收）"""

from __future__ import annotations

from arch_common import *  # noqa: F401,F403


class TestEntityFirst(unittest.TestCase):
    def test_thread_key_is_derived_and_stable(self):
        a = ThreadKey("owner", "mira", "together", "chat")
        b = ThreadKey("owner", "mira", "together", "chat")
        self.assertEqual(a.base, b.base)
        self.assertEqual(a.with_generation(1), f"{a.base}-1")
        self.assertNotEqual(a.base, ThreadKey("owner", "aster", "together", "chat").base)
        self.assertNotEqual(a.base, ThreadKey("owner", "mira", "trpg:1", "chat").base)

    def test_entity_thread_key_uses_scope(self):
        entity = CharacterEntity("mira")
        key = entity.thread_key("alice", "together", "chat")
        self.assertEqual(key.scope, "alice")
        self.assertEqual(key.entity, "mira")

    def test_entity_registry_default_and_unknown(self):
        registry = EntityRegistry()
        registry.register(CharacterEntity("mira", "ミラ", default=True))
        registry.register(CharacterEntity("aster", "アスター"))
        self.assertEqual(registry.default().id, "mira")
        self.assertIsNone(registry.get("nobody"))
        with self.assertRaises(ValueError):
            registry.register(CharacterEntity("mira"))

    def test_memory_scope_is_entity_scoped_not_device_scoped(self):
        scope = MemoryScope("owner", "mira", "together", "th_x-1")
        self.assertEqual(scope.bank(), "blueagent-mira")
        self.assertEqual(legacy_device_bank("nubia NX733J", "mira"),
                         "blueagent-nubia-nx733j-mira")
        # 換裝置不會換池：同一個實體永遠是同一本帳。
        self.assertEqual(MemoryScope("owner", "mira", "discord:1", "th_y-1").bank(),
                         scope.bank())

    def test_shared_and_private_banks_do_not_leak(self):
        owner = MemoryScope("owner", "mira", "together", "th_x-1")
        alice = MemoryScope("alice", "mira", "together", "th_y-1")
        self.assertEqual(owner.shared_bank(), "blueagent-mira")
        self.assertEqual(owner.private_bank(), "blueagent-mira-u-owner")
        self.assertEqual(alice.private_bank(), "blueagent-mira-u-alice")
        self.assertNotEqual(owner.private_bank(), alice.private_bank())
        self.assertEqual(owner.banks(), ("blueagent-mira",
                                         "blueagent-mira-u-owner"))


class TestPrincipals(unittest.TestCase):
    def test_registry_resolves_trusted_external_identity(self):
        registry = PrincipalRegistry()
        registry.register(Principal("alice", "Alice"))
        registry.bind("discord", "u-123", "alice")
        registry.bind_token("tok-a", "alice")
        self.assertEqual(registry.resolve(source="discord", external_id="u-123"), "alice")
        self.assertEqual(registry.resolve(source="phone", token="tok-a"), "alice")
        self.assertEqual(registry.resolve(source="phone"), "owner")
        self.assertEqual(registry.default(), "owner")

    def test_registry_rejects_unknown_binding(self):
        registry = PrincipalRegistry()
        with self.assertRaises(KeyError):
            registry.bind("discord", "u-1", "nobody")
        with self.assertRaises(ValueError):
            registry.bind("discord", "", "owner")


# ── B/C 自動切片與歸檔 ────────────────────────────────────────────────────────────
class TestAutoSegmentation(unittest.TestCase):
    def _thread_with_turns(self, at: float = 1000.0) -> Thread:
        thread = make_thread(now=at)
        thread.segment = [TurnRecord("t1", "user", "hello", "phone", at),
                          TurnRecord("t1", "assistant", "hi", "phone", at)]
        return thread

    def test_idle_gap_cuts(self):
        thread = self._thread_with_turns()
        self.assertEqual(cut_reason(thread, 1000.0 + 60, last_active=1000.0,
                                    policy=SegmentPolicy()), None)
        self.assertEqual(cut_reason(thread, 1000.0 + 1200, last_active=1000.0,
                                    policy=SegmentPolicy()), CutReason.IDLE)

    def test_day_boundary_cuts(self):
        t0 = time.mktime((2026, 10, 20, 23, 59, 0, 0, 0, -1))
        thread = self._thread_with_turns(t0)
        self.assertEqual(cut_reason(thread, t0 + 120, last_active=t0,
                                    policy=SegmentPolicy()), CutReason.DAY)

    def test_turns_and_chars_cut(self):
        thread = self._thread_with_turns()
        self.assertEqual(cut_reason(thread, 1000.0, last_active=1000.0,
                                    policy=SegmentPolicy(max_turns=2)), CutReason.TURNS)
        self.assertEqual(cut_reason(thread, 1000.0, last_active=1000.0,
                                    policy=SegmentPolicy(max_chars=3)), CutReason.CHARS)

    def test_seal_archives_and_resets_segment(self):
        thread = self._thread_with_turns()
        thread.carry_summary = "承接摘要"
        episode = seal(thread, 1001.0, CutReason.IDLE)
        self.assertIsNotNone(episode)
        self.assertEqual(episode.entity, "mira")
        self.assertEqual(episode.index, 1)
        self.assertEqual(thread.episode_index, 1)
        self.assertEqual(thread.segment, [])
        self.assertEqual(thread.carry_summary, "")
        self.assertTrue(episode.id.startswith("ep_mira-"))
        self.assertEqual(len(episode.turns), 2)

    def test_archive_calls_sink_and_keeps_history(self):
        sealed: list = []
        archive = EpisodeArchive(policy=SegmentPolicy(max_turns=2), sink=sealed.append)
        thread = self._thread_with_turns()
        self.assertEqual(archive.cut_if_needed(thread, 1000.0, last_active=1000.0),
                         CutReason.TURNS)
        episode = archive.seal(thread, 1001.0, CutReason.TURNS)
        self.assertIsNotNone(episode)
        self.assertEqual(len(sealed), 1)
        self.assertEqual(len(archive.all()), 1)


# ── D 路由／分流 ──────────────────────────────────────────────────────────────────



class TestIdempotencyWindow(unittest.TestCase):
    def test_ttl_and_capacity(self):
        window = IdempotencyWindow(ttl=10.0, max_keys=32)
        self.assertFalse(window.seen("a", 100.0))
        self.assertTrue(window.seen("a", 105.0))
        self.assertFalse(window.seen("a", 111.0))
        self.assertFalse(window.seen("", 100.0))
        for i in range(40):
            window.seen(f"k{i}", 200.0 + i)
        self.assertLessEqual(window.stats()["keys"], 32)


class TestEntityStore(unittest.TestCase):
    def test_presence_and_attention(self):
        store = EntityStore()
        store.mark_presence("mira", "phone", "together", SourceClass.INTERACTIVE, 100.0)
        store.mark_presence("mira", "discord", "discord:1", SourceClass.REALTIME, 110.0)
        store.mark_presence("mira", "phone", "together", SourceClass.INTERACTIVE, 120.0)
        presence = store.presence("mira")
        self.assertEqual(len(presence), 2)
        phone = next(p for p in presence if p.source == "phone")
        self.assertEqual(phone.since, 100.0)
        self.assertEqual(phone.last_seen, 120.0)
        store.attend("mira", "th_x-1", 120.0)
        self.assertEqual(store.attention("mira"), "th_x-1")
        self.assertEqual(store.attention("aster"), "")

    def test_presence_and_attention_carry_principal(self):
        store = EntityStore()
        store.mark_presence("mira", "discord", "discord:1", SourceClass.REALTIME,
                            100.0, principal="alice")
        presence = store.presence("mira")[0]
        self.assertEqual(presence.principal, "alice")
        store.attend("mira", "th_g-1", 100.0, principal="alice")
        self.assertEqual(store.attention("mira"), "th_g-1")
        self.assertEqual(store.attention_principal("mira"), "alice")

    def test_drop_stale_presence_keeps_memory(self):
        store = EntityStore()
        store.mark_presence("mira", "phone", "together", SourceClass.INTERACTIVE, 100.0)
        self.assertEqual(store.drop_stale(200.0, stale_after=50.0), [("mira", "phone")])
        self.assertEqual(store.presence("mira"), ())


# ── 入站主幹（端到端，但全替身） ──────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
