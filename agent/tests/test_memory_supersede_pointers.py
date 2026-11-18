#!/usr/bin/env python3
"""取代關係的哈希指針（owner 2026-11-01「標記取代最好留雙方哈希指針」）。

判據：

  ① 新的那條記 `supersedes`（指向舊的），舊的那條記 `superseded_by`（指向新的）⇒ **兩側都有**；
  ② 指針是**門牌**（`sha1(normalize_text)` 前 16 位），不是原文——跨倉／日誌裡都對得上；
  ③ 被取代的不刪、不進被動注入，但 `snapshot()` 還列得出來（`active=False`），
     而且**刪掉替代者就復活**（`revive`）；
  ④ 兩側指針走重建（jsonl → 索引）也不會掉；
  ⑤ 跨角色鏡像進來的那一條，她想起時帶「聽誰說的」前綴（轉述人稱）。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.domain.thread import MemoryScope, TurnRecord
from agent.brain.memory.fact_store import FactStore
from agent.brain.memory.facts import Fact, fact_key

OLD = "主人正在讀的書是《佐々木とピーちゃん》"
NEW = "主人正在讀的書是《另一本》"


class TestTwoWayPointers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.memory = SqliteMemory(self.root / "facts.db", seed=False,
                                   entity_ids=("mira", "aster"),
                                   entity_names={"aster": "アスター"})

    def put(self, text, **kwargs):
        return self.memory.add(text, layer="pair", audience={"mira", "owner"},
                               principal="owner", entity="mira", source="test",
                               **kwargs)

    def row(self, text):
        return self.memory.fact_by_text(text, entity="mira")

    def test_both_sides_carry_the_key(self):
        self.put(OLD, kind="state", slot="主人正在讀的書")
        new = self.put(NEW, kind="state", slot="主人正在讀的書",
                       supersedes=[fact_key(OLD)])
        old = self.row(OLD)
        self.assertEqual(new.supersedes, (fact_key(OLD),), "新的一側要記它取代了誰")
        self.assertEqual(old.superseded_by, fact_key(NEW), "舊的一側要記被誰取代")
        self.assertTrue(old.superseded_by.strip(), "指針不能是空的")
        self.assertEqual(len(new.key), 16)

    def test_keys_are_hashes_not_raw_text(self):
        fact = self.put(OLD)
        self.assertEqual(fact.key_value, fact_key(OLD))
        self.assertNotIn("主人", fact.key_value, "門牌不能是原文")

    def test_supersede_slot_also_writes_the_inbound_side(self):
        """既有順序（先 supersede_slot、再 add）也要補成雙向。"""
        self.put(OLD, kind="state", slot="主人正在讀的書")
        self.memory.supersede_slot(entity="mira", slot="主人正在讀的書", new_key=NEW)
        new = self.put(NEW, kind="state", slot="主人正在讀的書")
        self.assertEqual(self.row(OLD).superseded_by, new.key_value)
        self.assertEqual(new.supersedes, (fact_key(OLD),), "add 之後要收回 inbound 指針")

    def test_superseded_stays_listed_but_is_not_recalled(self):
        self.put(OLD, kind="state", slot="主人正在讀的書")
        self.put(NEW, kind="state", slot="主人正在讀的書", supersedes=[fact_key(OLD)])
        scope = MemoryScope(principal="owner", entity="mira", scene="dm", thread_id="t")
        recalled = [fact.text for fact in self.memory.recall_detail(
            scope, "主人正在讀的書", limit=5, participants={"owner", "mira"})]
        self.assertEqual(recalled, [NEW], "被取代的不該進被動注入")
        snapshot = self.memory.snapshot(participants={"owner"}, entity="mira")
        listed = {fact["text"]: fact for fact in snapshot["facts"]}
        self.assertIn(OLD, listed, "記憶頁要還看得到舊的")
        self.assertFalse(listed[OLD]["active"])
        self.assertEqual(listed[OLD]["superseded_by"], listed[NEW]["key"])
        self.assertEqual(snapshot["superseded"], 1)

    def test_deleting_the_replacement_revives_the_old_one(self):
        self.put(OLD, kind="state", slot="主人正在讀的書")
        new = self.put(NEW, kind="state", slot="主人正在讀的書", supersedes=[fact_key(OLD)])
        self.assertEqual(self.memory.revive(new.key_value), 1, "撤掉一個取代者要放回一條")
        old = self.row(OLD)
        self.assertEqual(old.superseded_by, "")
        self.assertEqual(old.valid_to, 0.0)
        self.assertEqual(self.row(NEW).supersedes, (), "復活後指針要收乾淨")

    def test_pointers_survive_a_rebuild(self):
        store = FactStore(self.root / "facts.jsonl")
        first = SqliteMemory(self.root / "facts_1.db", seed=False, store=store)
        first.add(OLD, layer="pair", entity="mira", source="test")
        first.add(NEW, layer="pair", entity="mira", source="test",
                  supersedes=[fact_key(OLD)])
        rebuilt = SqliteMemory(self.root / "facts_1.db", seed=False, store=store)
        rebuilt.rebuild()
        again = rebuilt.fact_by_text(NEW, entity="mira")
        self.assertIsNotNone(again, "重建後應該還查得到這條")
        self.assertEqual(again.supersedes, (fact_key(OLD),), "重建索引不能把指針弄丟")
        self.assertEqual(again.key_value, fact_key(NEW))

    def test_rebuild_roundtrip_preserves_all_stored_fields(self):
        """WP3：寫入覆蓋全部欄位的混合事實（含取代鏈、過期、桌上／現實各半）→ rebuild() → 逐欄比對。"""
        store = FactStore(self.root / "roundtrip.jsonl")
        mem = SqliteMemory(self.root / "roundtrip.db", seed=False, store=store)
        # 1. 現實狀態（先 supersede_slot，再寫新值）
        old_real = mem.add("主人正在讀的書是《第一卷》", layer="pair",
                           audience={"mira", "owner"}, label="提煉",
                           principal="owner", entity="mira", consensus=False,
                           self_owned=False, source="distill", kind="state",
                           valid_from=1_700_000_100.0, valid_to=0.0,
                           slot="主人正在讀的書", prior=0.82, stream="dialogue",
                           topic="reading", game=0, created_at=1_700_000_110.0)
        mem.supersede_slot(entity="mira", slot="主人正在讀的書",
                           new_key="主人正在讀的書是《第二卷》", at=1_700_000_500.0, game=0)
        mem.add("主人正在讀的書是《第二卷》", layer="pair",
                audience={"mira", "owner"}, label="提煉",
                principal="owner", entity="mira", consensus=True,
                self_owned=False, source="distill", kind="state",
                valid_from=1_700_000_500.0, valid_to=0.0,
                slot="主人正在讀的書", prior=0.91, stream="dialogue",
                topic="reading", game=0, created_at=1_700_000_510.0)
        # 2. 現實已過期事件（self_owned + stream=life）
        mem.add("今天下午整理了社團房間的書架", layer="self",
                audience={"mira"}, label="日記",
                principal="mira", entity="mira", consensus=False,
                self_owned=True, source="self_note", kind="event",
                valid_from=1_700_001_000.0, valid_to=1_700_087_400.0,
                slot="", prior=0.75, stream="life",
                topic="tidy", game=0, created_at=1_700_001_005.0)
        # 3. 桌上事實（顯式 supersedes 取代鏈，audience=None 世界層 + circle 層）
        old_trpg = mem.add("港町的北門現在由衛兵封鎖", layer="world",
                           audience=None, label="桌上",
                           principal="owner", entity="mira", consensus=True,
                           self_owned=False, source="trpg:table-1", kind="state",
                           valid_from=1_700_010_000.0, valid_to=0.0,
                           slot="北門狀態", prior=0.88, stream="dialogue",
                           topic="gate", game=1, created_at=1_700_010_020.0)
        mem.add("港町的北門已經被調查員打開", layer="circle",
                audience={"mira", "aster", "owner"}, label="桌上更新",
                principal="owner", entity="mira", consensus=False,
                self_owned=False, source="trpg:table-1", kind="state",
                valid_from=1_700_020_000.0, valid_to=0.0,
                slot="北門狀態", supersedes=[old_trpg.key], prior=0.95,
                stream="dialogue", topic="gate", game=1, created_at=1_700_020_030.0)
        # 4. 桌上已過期事件
        mem.add("今晚在酒館集合交換情報", layer="pair",
                audience={"mira", "owner"}, label="桌上事件",
                principal="owner", entity="aster", consensus=False,
                self_owned=False, source="trpg:table-1", kind="event",
                valid_from=1_700_030_000.0, valid_to=1_700_116_400.0,
                slot="", prior=0.66, stream="dialogue",
                topic="tavern", game=1, created_at=1_700_030_010.0)
        self.assertTrue(old_real.key)
        # 記一筆使用次數（驗證除了 utility / misses / last_used_at 之外全一致）
        mem.note_usage(["主人正在讀的書是《第二卷》"], used=True, at=1_700_050_000.0)

        fields = (
            "text", "norm", "layer", "audience", "label", "principal", "entity",
            "consensus", "self_owned", "source", "created_at", "kind",
            "valid_from", "valid_to", "superseded_by", "supersedes", "key",
            "slot", "prior", "shingles", "stream", "topic", "game",
        )
        before_rows = {
            (row["entity"], row["key"]): {col: row[col] for col in fields}
            for row in mem._db.execute("SELECT * FROM facts ORDER BY id").fetchall()
        }
        before_jsonl_lines = len(store.load())
        self.assertEqual(len(before_rows), 6)

        rebuilt_count = mem.rebuild()
        self.assertEqual(rebuilt_count, 6)
        self.assertEqual(len(store.load()), before_jsonl_lines, "rebuild 不應向 facts.jsonl 重複追加")

        after_rows = {
            (row["entity"], row["key"]): {col: row[col] for col in fields}
            for row in mem._db.execute("SELECT * FROM facts ORDER BY id").fetchall()
        }
        self.assertEqual(after_rows, before_rows)


class TestAppendOnlyFactStore(unittest.TestCase):
    """R1：索引可以重建，正本只能追加；索引外的行也不能被維護操作吃掉。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.store = FactStore(self.root / "facts.jsonl")
        self.memory = SqliteMemory(self.root / "facts.db", store=self.store, seed=False)
        self.addCleanup(self.memory.close)

    def put_only_in_store(self, text="只存在正本、尚未進索引的事實"):
        fact = Fact(text=text, layer="pair", audience=frozenset({"mira", "owner"}),
                    principal="owner", entity="mira", source="test", created_at=1.0)
        self.store.append(fact)
        return fact

    def assert_original_prefix_unchanged(self, original):
        current = self.store.path.read_bytes()
        self.assertEqual(current[:len(original)], original, "既有正本行一個位元組都不能改")
        self.assertTrue(current.endswith(b"\n"), "每次追加都必須是完整的一行")

    def test_slot_retire_and_revive_preserve_out_of_index_fact(self):
        old = self.memory.add(OLD, entity="mira", kind="state", slot="reading")
        orphan = self.put_only_in_store()
        original = self.store.path.read_bytes()

        self.assertEqual(self.memory.supersede_slot(
            entity="mira", slot="reading", new_key=NEW, at=100.0), 1)
        new = self.memory.add(NEW, entity="mira", kind="state", slot="reading")
        self.assertEqual(self.memory.revive(new.key), 1)
        self.assert_original_prefix_unchanged(original)
        self.assertIn(orphan.text, [fact.text for fact in self.store.load()])

        before_rebuild = self.store.path.read_bytes()
        self.assertEqual(self.memory.rebuild(), 3)
        self.assertEqual(self.store.path.read_bytes(), before_rebuild, "重建本身也不能改正本")
        self.assertIsNotNone(self.memory.fact_by_text(orphan.text, entity="mira"))
        self.assertEqual(self.memory.fact_by_text(OLD, entity="mira").superseded_by, "")
        self.assertEqual(self.memory.fact_by_text(NEW, entity="mira").supersedes, ())
        self.assertEqual(old.key, fact_key(OLD))

    def test_explicit_supersede_record_replays_without_rewriting_facts(self):
        old = self.memory.add(OLD, entity="mira")
        orphan = self.put_only_in_store("明確取代也要保留的索引外事實")
        original = self.store.path.read_bytes()

        self.assertEqual(self.memory.supersede(
            entity="mira", keys=[old.key], new_key=NEW, at=123.0), 1)
        new = self.memory.add(NEW, entity="mira")
        self.assert_original_prefix_unchanged(original)
        before_rebuild = self.store.path.read_bytes()
        self.memory.rebuild()

        rebuilt_old = self.memory.fact_by_text(OLD, entity="mira")
        rebuilt_new = self.memory.fact_by_text(NEW, entity="mira")
        self.assertEqual(rebuilt_old.superseded_by, new.key)
        self.assertEqual(rebuilt_old.valid_to, 123.0)
        self.assertEqual(rebuilt_new.supersedes, (old.key,))
        self.assertIsNotNone(self.memory.fact_by_text(orphan.text, entity="mira"))
        self.assertEqual(self.store.path.read_bytes(), before_rebuild)

    def test_old_loader_skips_operation_and_unknown_rows(self):
        fact = Fact(text="舊版仍要讀得到的事實", entity="mira")
        rows = [fact.to_store_dict(), {"op": "future_operation", "value": 1},
                {"op": "revive", "key": fact_key(OLD)}, {"key": "no-text"}]
        self.store.path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\nnot-json\n",
            encoding="utf-8")
        self.assertEqual([item.text for item in self.store.load()], [fact.text])
        records = self.store.load_records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1], {"op": "revive", "key": fact_key(OLD)})

    def test_hundreds_of_concurrent_appends_are_complete_lines(self):
        workers, each = 8, 30

        def write_facts(worker):
            for index in range(each):
                serial = worker * each + index
                self.memory.add(f"並發事實 {serial:03d}", entity="mira",
                                kind="state", slot=f"slot-{index % 6}",
                                created_at=1_000.0 + serial)
                self.memory.supersede_slot(
                    entity="mira", slot=f"slot-{index % 6}",
                    new_key=f"下一版 {serial:03d}", at=2_000.0 + serial)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(write_facts, range(workers)))

        raw = self.store.path.read_bytes()
        self.assertTrue(raw.endswith(b"\n"), "最後一行也不能只寫一半")
        lines = raw.splitlines()
        self.assertGreaterEqual(len(lines), workers * each)
        for line in lines:
            self.assertIsInstance(json.loads(line), dict)
        self.assertEqual(len(self.store.load()), workers * each,
                         "並發追加不能丟事實行")
        self.assertEqual(self.memory.rebuild(), workers * each)
        self.assertEqual(self.store.path.read_bytes(), raw)


class TestHearsay(unittest.TestCase):
    """跨角色：她聽另一個角色說的 ⇒ 想起時是轉述，不是「你告訴我的」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = SqliteMemory(pathlib.Path(self.tmp.name) / "facts.db", seed=False,
                                   entity_ids=("mira", "aster"),
                                   entity_names={"aster": "アスター"})

    def test_mirrored_fact_is_recalled_as_hearsay(self):
        # 主人對 aster 說了「記住…」、mira 也在場 ⇒ aster 的倉是直接記憶，
        # mira 的倉是「aster 聽到的」那一份（record 的鏡像寫入）。
        said = "猫を飼っているって記住"
        self.memory.record(
            MemoryScope(principal="owner", entity="aster", scene="dm", thread_id="t_x"),
            TurnRecord(turn_id="t1", role="user", text=said, source="t", at=1.0),
            participants={"owner", "mira"})
        scope = MemoryScope(principal="owner", entity="mira", scene="dm", thread_id="t")
        facts = self.memory.recall_detail(scope, said, limit=5,
                                          participants={"owner", "mira"})
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].hearsay_from, "アスター")
        self.assertTrue(facts[0].recall_text.startswith("（アスターから聞いた話）"),
                        facts[0].recall_text)

    def test_own_memory_is_not_marked(self):
        self.memory.add("主人は猫を飼っている", layer="pair", audience={"mira", "owner"},
                        principal="owner", entity="mira", source="explicit")
        scope = MemoryScope(principal="owner", entity="mira", scene="dm", thread_id="t")
        facts = self.memory.recall_detail(scope, "猫を飼っている", limit=5,
                                          participants={"owner", "mira"})
        self.assertEqual(facts[0].hearsay_from, "")
        self.assertEqual(facts[0].recall_text, facts[0].text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
