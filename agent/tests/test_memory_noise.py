#!/usr/bin/env python3
"""召回不再塞「底噪」＋生產不落 demo 種子（owner 2026-10-23「先做 1,3」）。

① 種子：生產一律不播種（沒有 `BLUEAGENT_SEED` 了）；只有傳播種函式（測試的 demo_people）才落種子。
③ 底噪：查詢與任何事實都沒有二字重疊時，召回必須是**空**（以前會回 5 條無關事實）。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.memory.fact_store import FactStore
from agent.brain.memory.in_memory import InMemoryMemory
from agent.brain.domain.thread import MemoryScope


def scope(entity: str = "mira", principal: str = "owner") -> MemoryScope:
    return MemoryScope(principal=principal, entity=entity, scene="chat",
                       thread_id="th-noise")


class TestNoBaselineNoise(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def _fill(self, memory):
        memory.add("主人在读《佐々木とピーちゃん》，读到第六卷。",
                   layer="pair", audience={"mira", "owner"}, entity="mira")
        memory.add("窗边有一盆薄荷。", layer="world", entity="mira")

    def test_inmemory_returns_empty_when_nothing_overlaps(self):
        memory = InMemoryMemory(seed=False)
        self._fill(memory)
        got = memory.recall_detail(scope(), "宇宙飞船发动机", limit=5)
        self.assertEqual(got, [], [f.text for f in got])

    def test_inmemory_still_hits_when_overlapping(self):
        memory = InMemoryMemory(seed=False)
        self._fill(memory)
        got = [f.text for f in memory.recall_detail(scope(), "小说 第六卷", limit=5)]
        self.assertTrue(any("佐々木" in text for text in got), got)

    def test_sqlite_returns_empty_when_nothing_overlaps(self):
        store = FactStore(self.root / "facts.jsonl")
        memory = SqliteMemory(self.root / "facts.db", store=store, seed=False)
        self._fill(memory)
        got = memory.recall_detail(scope(), "宇宙飞船发动机", limit=5)
        self.assertEqual(got, [], [f.text for f in got])


class TestSeedSwitch(unittest.TestCase):
    def test_default_has_no_demo_facts(self):
        # 生產不播種：沒給播種函式就是空庫（`seed=True` 這種舊寫法也不再帶 demo 資料進來）
        for memory in (InMemoryMemory(), InMemoryMemory(seed=False), InMemoryMemory(seed=True)):
            self.assertEqual(len(memory._facts), 0)      # noqa: SLF001 - 測試要看內部
            self.assertEqual(memory.snapshot()["total"], 0)

    def test_seed_callable_is_used_for_tests(self):
        import demo_people
        memory = InMemoryMemory(seed=demo_people.seed_facts)
        self.assertGreater(memory.snapshot()["total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
