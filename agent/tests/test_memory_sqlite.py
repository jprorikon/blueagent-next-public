#!/usr/bin/env python3
"""記憶索引後端（SQLite FTS5）：行為與記憶體版一致，但不再每輪掃全表。

判據：
  ① **一致性**：同一份種子資料、同一句 query，兩個後端召回的事實逐條相同（golden 對拍）；
  ② **可見域**：pair/private 在「在場的人 ⊄ audience」時一條都撈不到（guest 讀不到主人的私密）；
  ③ **原文優先**：刪掉 `facts.db`、只留 `facts.jsonl` 也能重建，召回不變；
  ④ **去重**：同一句話（正規化後相同）只存一條；
  ⑤ **規模**：10k 條事實下 recall P95 < 50 ms（本機實測，含 SQLite 開銷）。
"""

from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.memory.fact_store import FactStore
from agent.brain.memory.in_memory import InMemoryMemory
from agent.brain.domain.thread import MemoryScope, TurnRecord

NAMES = {"owner": "主人", "alice": "Alice", "bob": "Bob"}
QUERIES = ["薄荷", "空调 温度", "猫", "薄荷水", "吃饭", "工作"]
OWNER = MemoryScope(principal="owner", entity="mira", scene="together",
                    thread_id="th-test")


def seeds() -> list[dict]:
    """一份覆盖各层的小语料（世界/共识/圈子/两人/私密/自己）。

    2026-10-23 起 **audience 一律含她自己**（她是自己房間的固定成員）：
    「她 + 1 人」＝pair、「她 + 2 人以上」＝circle。
    """
    return [
        dict(text="主人怕冷，空调一般开在 26 度。", layer="world"),
        dict(text="窗边有一盆薄荷，是主人买的。", layer="world"),
        dict(text="大家都说好不在工作时间聊太久。", layer="consensus",
             audience={"mira", "owner", "alice", "bob"}, consensus=True),
        dict(text="Alice 不吃香菜。", layer="circle",
             audience={"mira", "owner", "alice"}),
        dict(text="主人和 Alice 上周一起去吃了火锅。", layer="circle",
             audience={"mira", "owner", "alice"}),
        dict(text="主人和她约好周末一起回老家。", layer="pair",
             audience={"mira", "owner"}),
        dict(text="主人养了一只叫团子的猫。", layer="private",
             audience={"mira", "owner"}, principal="owner"),
        # 房間＝`rooms.self_room` 的默認（只有她自己）：她自己記下的事
        dict(text="她自己记下：今天想早一点睡。", layer="self",
             audience={"mira"}, self_owned=True),
        # 從主人在場的對話寫成的 self（reflection 的日記就是這種）：房間裡有主人
        dict(text="她的日记：主人说最近在准备搬家。", layer="self",
             audience={"mira", "owner"}, self_owned=True),
    ]


def make_memory(tmp: pathlib.Path, *, backend: str):
    store = FactStore(tmp / "facts.jsonl")
    if backend == "sqlite":
        memory = SqliteMemory(tmp / "facts.db", store=store, names=NAMES, seed=False)
    else:
        memory = InMemoryMemory(names=NAMES, store=store)
        memory.clear()
    for row in seeds():
        memory.add(**row)
    return memory


def scope(principal: str = "owner") -> MemoryScope:
    return MemoryScope(principal=principal, entity="mira", scene="together",
                        thread_id=f"th-{principal}")


class TestParityWithMemoryBackend(unittest.TestCase):
    def test_recall_matches_on_every_query(self):
        for query in QUERIES:
            for participants in (["owner"], ["owner", "alice"], ["bob"]):
                with tempfile.TemporaryDirectory() as left, \
                        tempfile.TemporaryDirectory() as right:
                    fast = make_memory(pathlib.Path(left), backend="sqlite")
                    slow = make_memory(pathlib.Path(right), backend="memory")
                    a = [fact.text for fact in fast.recall_detail(
                        scope("owner"), query, limit=4, participants=participants)]
                    b = [fact.text for fact in slow.recall_detail(
                        scope("owner"), query, limit=4, participants=participants)]
                    self.assertEqual(a, b, f"query={query!r} room={participants}")

    def test_snapshot_shape_is_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = make_memory(pathlib.Path(tmp), backend="sqlite")
            snapshot = memory.snapshot(participants=["owner"])
            self.assertEqual(snapshot["total"], len(seeds()))
            self.assertIn("visible_counts", snapshot)
            self.assertIn("hidden_counts", snapshot)
            self.assertEqual(snapshot["backend"], "sqlite")


class TestVisibility(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = make_memory(pathlib.Path(self.tmp.name), backend="sqlite")

    def texts(self, query: str, participants, principal="owner") -> list[str]:
        return [fact.text for fact in self.memory.recall_detail(
            scope(principal), query, limit=10, participants=participants)]

    def test_private_fact_hidden_from_other_people(self):
        self.assertFalse(any("团子" in text for text in self.texts("猫", ["alice"])))

    def test_pair_fact_needs_its_audience(self):
        """披露層只看子集：`在場的人（含她） ⊆ 房間成員` 才可說。

        火鍋那條的房間是她＋主人＋Alice ⇒ 主人在場可說、Alice 在場也可說（她在那間房），
        但 bob 從沒進過那間房 ⇒ 一條都撈不到。
        """
        self.assertTrue(any("火锅" in text for text in self.texts("火锅", ["owner"])))
        self.assertTrue(any("火锅" in text for text in self.texts("火锅", ["owner", "alice"])))
        self.assertFalse(any("火锅" in text for text in self.texts("火锅", ["bob"])))

    def test_world_fact_visible_to_everyone(self):
        self.assertTrue(any("薄荷" in text for text in self.texts("薄荷", ["bob"])))

    def test_self_layer_is_hers_even_in_front_of_outsiders(self):
        """她自己那層（`layer="self"`）：她**永遠想得起來**——要不要說由她定，不用權限表擋。

        owner 2026-11-03「線路 A」：她和主人、和仅她自己 是两个域，self 是她的私域；
        擋住的是「想」就等於連選擇都沒有，所以 recall 不放閘，改由〔私記〕標記＋她自己的判斷。
        """
        self.assertTrue(any("早一点睡" in text for text in self.texts("一点", ["owner"])))
        self.assertTrue(any("早一点睡" in text for text in self.texts("一点", ["bob"])))

    def test_self_fact_from_a_room_with_others_stays_gated(self):
        """線路 A 只放「房間裡只有她」的 self（2026-11-03 review）：從主人在場的對話寫成的 self
        （日記）房間裡有主人 ⇒ 照舊走披露閘，不然主人的私事會在客人面前被召回。"""
        self.assertTrue(any("搬家" in text for text in self.texts("搬家", ["owner"])))
        self.assertFalse(any("搬家" in text for text in self.texts("搬家", ["bob"])))

    def test_guest_cannot_read_owner_private(self):
        guest = MemoryScope(principal="guest-x", entity="mira", scene="together",
                           thread_id="th-guest")
        texts = [fact.text for fact in self.memory.recall_detail(
            guest, "猫", limit=10, participants=["guest-x"])]
        self.assertFalse(any("团子" in text for text in texts))


class TestStorage(unittest.TestCase):
    def test_rebuild_from_jsonl_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            memory = make_memory(root, backend="sqlite")
            before = [fact.text for fact in memory.recall_detail(
                scope(), "薄荷", limit=4, participants=["owner"])]
            memory.close()
            (root / "facts.db").unlink()
            again = SqliteMemory(root / "facts.db", store=FactStore(root / "facts.jsonl"),
                                 names=NAMES, seed=False)
            after = [fact.text for fact in again.recall_detail(
                scope(), "薄荷", limit=4, participants=["owner"])]
            self.assertEqual(before, after, "索引重建后召回必须一致")
            self.assertEqual(again.count(), len(seeds()))

    def test_duplicate_text_is_stored_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = SqliteMemory(pathlib.Path(tmp) / "facts.db", seed=False)
            memory.add("主人喜欢薄荷。", layer="world")
            memory.add("主人喜欢薄荷。", layer="world")
            self.assertEqual(memory.count(), 1)

    def test_record_uses_shared_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = SqliteMemory(pathlib.Path(tmp) / "facts.db", names=NAMES, seed=False)
            turn = TurnRecord(turn_id="t1", role="user", text="我喜欢薄荷",
                              source="phone:owner", at=0.0, speaker="owner")
            memory.record(scope(), turn, participants=["owner"])
            texts = [fact.text for fact in memory.recall_detail(
                scope(), "薄荷", limit=5, participants=["owner"])]
            self.assertTrue(any("薄荷" in text for text in texts), texts)

    def test_jsonl_is_written_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            memory = make_memory(root, backend="sqlite")
            lines = (root / "facts.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len([line for line in lines if line.strip()]), len(seeds()))


class TestScale(unittest.TestCase):
    def test_10k_facts_recall_is_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = SqliteMemory(pathlib.Path(tmp) / "facts.db", seed=False)
            layers = ("world", "consensus", "circle", "pair", "private")
            for index in range(10_000):
                memory.add(f"第 {index} 号记录：主人和 Alice 在今天聊到了第 {index} 件事",
                           layer=layers[index % len(layers)],
                           audience=None if index % len(layers) < 2 else
                           {"owner", "alice"} if index % len(layers) == 3 else {"owner"})
            self.assertEqual(memory.count(), 10_000)
            timings = []
            for index in range(20):
                started = time.perf_counter()
                memory.recall_detail(scope(), f"第 {index * 13} 件事", limit=4,
                                     participants=["owner"])
                timings.append((time.perf_counter() - started) * 1000)
            timings.sort()
            p95 = timings[int(len(timings) * 0.95) - 1]
            self.assertLess(p95, 50, f"P95={p95:.1f}ms 太慢：{timings}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
