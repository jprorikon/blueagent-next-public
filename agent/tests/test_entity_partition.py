#!/usr/bin/env python3
"""按實體分倉（owner 2026-10-23：「需要按实体分仓」）。

三條要釘住的：
  1. **分倉**：`mira` 倉的事實在 `aster` 的回合裡撈不到（反之亦然）。
  2. **跨角色鏡像**：她 ↔ 另一個角色 agent 的一輪 ⇒ **雙方各按自己的角度落一份**
     （兩個倉各一條；對方那一份的 `principal` 是「這一側的 entity」）。
  3. **舊資料相容**：分倉前寫的（`entity=""`）仍可被任何倉看到，遷移腳本負責補真值。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.memory.in_memory import InMemoryMemory
from agent.brain.domain.thread import MemoryScope, TurnRecord

ENTITY_IDS = ("mira", "aster", "nori")


def scope(entity: str, principal: str = "owner") -> MemoryScope:
    return MemoryScope(principal=principal, entity=entity, scene="together",
                       thread_id=f"th-{entity}-{principal}")


def memory() -> InMemoryMemory:
    mem = InMemoryMemory(entity_ids=ENTITY_IDS)
    mem.clear()
    return mem


class TestPartition(unittest.TestCase):
    def test_bins_are_separate(self):
        mem = memory()
        mem.add("她在倉庫裡藏了一把傘。", layer="self", self_owned=True,
                entity="mira")
        mem.add("她在港口撿到一枚硬幣。", layer="self", self_owned=True,
                entity="aster")
        mus = [f.text for f in mem.recall_detail(scope("mira"), "撿到", limit=10)]
        aster = [f.text for f in mem.recall_detail(scope("aster"), "撿到", limit=10)]
        self.assertFalse(any("硬幣" in t for t in mus), mus)
        self.assertTrue(any("硬幣" in t for t in aster), aster)
        self.assertFalse(any("傘" in t for t in aster), aster)

    def test_legacy_rows_stay_visible(self):
        mem = memory()
        mem.add("舊資料：窗邊有一盆薄荷。", layer="world")   # entity=""
        for entity in ENTITY_IDS:
            got = [f.text for f in mem.recall_detail(scope(entity), "薄荷", limit=5)]
            self.assertTrue(any("薄荷" in t for t in got), (entity, got))

    def test_cross_entity_turn_writes_both_sides(self):
        mem = memory()
        turn = TurnRecord(turn_id="t1", role="user", speaker="mira",
                          text="记住 我和 aster 说好了，下次一起去看海",
                          source="chat", at=0.0)
        mem.record(scope("mira", principal="mira"), turn,
                   participants=["mira", "aster"])
        # 各自「在自己的回合裡」想起來（在場的人只有她自己那邊的成員）
        mus = [f for f in mem.recall_detail(scope("mira", principal="mira"),
                                            "看海", limit=10)]
        aster = [f for f in mem.recall_detail(scope("aster", principal="aster"),
                                            "看海", limit=10)]
        self.assertTrue(mus, "她自己的倉要有一份")
        self.assertTrue(aster, "aster 的倉也要有一份（各自角度）")
        mirrored = [f for f in aster if f.source.endswith("+mirror")]
        self.assertTrue(mirrored, [f.source for f in aster])
        self.assertEqual(mirrored[0].principal, "mira")
        self.assertEqual(mirrored[0].entity, "aster")
        # 兩邊的房間成員一致（都含雙方）
        self.assertEqual(set(mirrored[0].audience), {"mira", "aster"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
