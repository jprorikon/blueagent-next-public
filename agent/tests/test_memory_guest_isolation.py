#!/usr/bin/env python3
"""可見性＝**披露層**，owner 2026-10-23 拍板：只看子集，且 audience 含她自己。

規則（只有一道）：

    在場的人（含她）⊆ 這條記憶的房間成員  ⇔  可說出來

- 她的回合裡她一定在場 ⇒ `P` 一定含 `mira`。
- 她一個人時 `{她} ⊆ 任何含她的房間` 恆成立 ⇒ 她自己的記憶永遠想得起來（歸屬層不做過濾）。
- 客人單獨在場時，只有「那間房裡有他」的記憶才說得出來；不在房裡的封口（記得，但不提）。

判據表（audience 都含她）：

| 事實 | 房間 | 在場 | 可說 |
|---|---|---|---|
| pair | {她, owner} | {她, owner} | ✅ |
| pair | {她, owner} | {她, alice} | ❌ alice 不在房裡 |
| pair | {她, owner} | {她, owner, alice} | ❌ 多一個第三者（超出房間） |
| circle | {她, alice, bob} | {她, alice} | ✅ 子集即可 |
| circle | {她, alice, bob} | {她, alice, bob} | ✅ |
| private | {她, alice} | {她, alice} | ✅ |
| private | {她, alice} | {她, bob} | ❌ |
| world | — | 任何人 | ✅ |
| self | {她, owner} | {她, bob} | ❌ 她記得，但不會講給不在場的人 |
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.memory.in_memory import InMemoryMemory
from agent.brain.domain.thread import MemoryScope


def scope(principal: str) -> MemoryScope:
    return MemoryScope(principal=principal, entity="mira", scene="together",
                       thread_id=f"th-{principal}")


class TestDisclosureSubset(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = InMemoryMemory(names={"owner": "主人", "alice": "Alice", "bob": "Bob"})
        self.memory.clear()
        self.memory.add("主人和她周末要回老家。", layer="pair",
                        audience={"mira", "owner"})
        self.memory.add("Alice 和 Bob 在准备一个惊喜。", layer="circle",
                        audience={"mira", "alice", "bob"})
        self.memory.add("Alice 不吃香菜。", layer="private",
                        audience={"mira", "alice"}, principal="alice")
        self.memory.add("窗边有一盆薄荷。", layer="world")
        self.memory.add("她自己记下：今天想早点睡。", layer="self",
                        audience={"mira", "owner"}, self_owned=True)

    def seen(self, query: str, speaker: str, people) -> list[str]:
        return [fact.text for fact in self.memory.recall_detail(
            scope(speaker), query, limit=10, participants=people)]

    # ── 她 + 主人的兩人房 ────────────────────────────────────────────────
    def test_owner_alone_can_read_pair(self):
        self.assertTrue(any("回老家" in text
                            for text in self.seen("周末", "owner", {"owner"})))

    def test_guest_outside_the_room_cannot_read(self):
        self.assertFalse(any("回老家" in text
                             for text in self.seen("周末", "alice", {"alice"})),
                         "Alice 不在那間房裡 ⇒ 不該說出來")

    def test_extra_person_closes_it(self):
        self.assertFalse(any("回老家" in text for text in self.seen(
            "周末", "owner", {"owner", "alice"})),
            "超出房間成員（多一個第三者）⇒ 不說")

    # ── 她 + 兩位客人的圈子 ────────────────────────────────────────────
    def test_subset_of_the_room_can_read(self):
        self.assertTrue(any("惊喜" in text
                            for text in self.seen("惊喜", "alice", {"alice"})),
                        "Alice 在那間房裡 ⇒ 子集即可，能說")

    def test_everyone_in_the_room_can_read(self):
        self.assertTrue(any("惊喜" in text
                            for text in self.seen("惊喜", "bob", {"alice", "bob"})))

    # ── private / world / self ──────────────────────────────────────────
    def test_own_private_is_readable(self):
        self.assertTrue(any("香菜" in text
                            for text in self.seen("香菜", "alice", {"alice"})))

    def test_other_guest_private_is_not(self):
        self.assertFalse(any("香菜" in text
                             for text in self.seen("香菜", "bob", {"bob"})))

    def test_world_is_open(self):
        self.assertTrue(any("薄荷" in text for text in self.seen("薄荷", "bob", {"bob"})))

    def test_self_memory_is_kept_but_not_told(self):
        """她自己記得（歸屬層），但不在那間房的人面前不提（披露層）。"""
        self.assertFalse(any("早" in text for text in self.seen("早点", "bob", {"bob"})))
        self.assertTrue(any("早" in text for text in self.seen("早点", "owner", {"owner"})))

    # ── snapshot（UI）要跟檢索同一答案 ──────────────────────────────────
    def test_snapshot_visibility_matches_recall(self):
        view = {fact["text"]: fact["visible"]
                for fact in self.memory.snapshot({"alice"})["facts"]}
        self.assertFalse(view["主人和她周末要回老家。"])
        self.assertTrue(view["Alice 和 Bob 在准备一个惊喜。"])
        self.assertTrue(view["Alice 不吃香菜。"])
        self.assertTrue(view["窗边有一盆薄荷。"])
        self.assertFalse(view["她自己记下：今天想早点睡。"])   # 她記得，但 alice 不在那間房


class TestRuleInsideAFullTurn(unittest.TestCase):
    """走完整一輪（不是直接叫記憶體）：guest 問「主人的事」，召回裡不能出現。"""

    def test_guest_turn_does_not_recall_owner_pair(self):
        state = make_state()
        self.addCleanup(state.shutdown)
        state.send("记住：主人和 Alice 周末要回老家", entity="mira",
                   principal="owner", participants=["owner"])
        guest = state.join_guest(invite_code="blueagent", discord_id="999",
                                 display_name="Alice")
        pid = state.approve_guest(guest["request_id"], final_kind="friend")[
            "request"]["principal_id"]
        result = state.send("周末有什么安排吗？", entity="mira", principal=pid)
        memory = next(f for f in result["frames"] if f["kind"] == "memory")
        self.assertFalse(any("回老家" in str(text) for text in memory["payload"]["facts"]),
                         memory["payload"]["facts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
