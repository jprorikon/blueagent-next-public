#!/usr/bin/env python3
"""單值槽位只留一條現值，**按時間關係刷新**（owner 2026-11-16；架構審核 D1）。

判據：
  ① 同槽、同場景寫進較新的 ⇒ 舊的被收掉、新的是現值（原有行為，不能壞）；
  ② **較舊的晚到**（補跑舊窗口、回填原文）⇒ 現值不動，晚到的那條一落庫就是「已被取代」，
     兩側指針齊全、失效時間＝現值成立的時刻；
  ③ 庫裡同槽已有多條現值（存量碎片）⇒ 下一次寫入時收成一條，留最新的；
  ④ 只有「狀態」與稱呼是單值：好惡／約定誤帶了 slot 也不互相收掉；
  ⑤ 場景不互相收（桌上的不收現實的）；
  ⑥ 從正本重建之後結果一樣；
  ⑦ 蒸餾不再先 `supersede_slot`：窗口亂序處理，現值仍是時間最新的那條；
  ⑧ 提煉 prompt 帶上現用的槽位名（同場景、去掉稱呼槽），沒有就不加任何字。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.memory.fact_store import FactStore
from agent.brain.memory.facts import ADDRESS_SLOT, single_valued
from agent.brain.memory.in_memory import InMemoryMemory
from agent.brain.runtime.memory.distill_support import (apply_candidates, slot_hint,
                                                        with_slot_hint)

SLOT = "主人がいま読んでいる本"
OLD, MID, NEW = "主人は一巻を読んでいる", "主人は三巻を読んでいる", "主人は六巻を読んでいる"
PAIR = {"mira", "owner"}


class Backends(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.round = 0

    def backends(self):
        self.round += 1
        yield "sqlite", SqliteMemory(self.root / f"s{self.round}" / "facts.db", seed=False)
        yield "memory", InMemoryMemory()

    @staticmethod
    def write(memory, text, at, *, kind="state", slot=SLOT, game=0, **kwargs):
        return memory.add(text, layer="pair", entity="mira", audience=PAIR, source="distill",
                          kind=kind, slot=slot, valid_from=at, game=game, **kwargs)

    @staticmethod
    def row(memory, text) -> dict:
        return next(row for row in memory.snapshot(None, entity="mira")["facts"]
                    if row["text"] == text)

    def active(self, memory, slot=SLOT) -> list[str]:
        return sorted(row["text"] for row in memory.snapshot(None, entity="mira")["facts"]
                      if row["slot"] == slot and row["active"])


class TestSingleValuedPredicate(unittest.TestCase):
    def test_only_states_and_address(self):
        self.assertTrue(single_valued("state", SLOT))
        self.assertTrue(single_valued("rule", ADDRESS_SLOT))
        self.assertFalse(single_valued("preference", SLOT))
        self.assertFalse(single_valued("state", ""))
        self.assertFalse(single_valued("event", "今夜の予定"))


class TestTimeOrderedSlot(Backends):
    def test_newer_value_replaces_the_current_one(self):
        for name, memory in self.backends():
            with self.subTest(name):
                old = self.write(memory, OLD, 100.0)
                new = self.write(memory, NEW, 200.0)
                self.assertEqual(self.active(memory), [NEW])
                self.assertEqual(self.row(memory, OLD)["superseded_by"], new.key)
                self.assertEqual(self.row(memory, NEW)["supersedes"], [old.key])

    def test_older_value_arriving_late_does_not_win(self):
        for name, memory in self.backends():
            with self.subTest(name):
                new = self.write(memory, NEW, 200.0)
                late = self.write(memory, OLD, 100.0)
                self.assertEqual(self.active(memory), [NEW], "現值是時間最新的，不是最後寫的")
                self.assertEqual(late.superseded_by, new.key)
                self.assertEqual(late.valid_to, 200.0, "失效時間＝現值成立的時刻")
                self.assertEqual(self.row(memory, OLD)["superseded_by"], new.key)
                self.assertEqual(self.row(memory, NEW)["supersedes"], [late.key])

    def test_three_values_in_any_order_keep_the_latest(self):
        for order in ((OLD, MID, NEW), (NEW, OLD, MID), (MID, NEW, OLD), (NEW, MID, OLD)):
            times = {OLD: 100.0, MID: 150.0, NEW: 200.0}
            for name, memory in self.backends():
                with self.subTest(f"{name}/{order}"):
                    for text in order:
                        self.write(memory, text, times[text])
                    self.assertEqual(self.active(memory), [NEW])

    def test_same_time_latest_write_wins(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.write(memory, OLD, 100.0)
                self.write(memory, NEW, 100.0)
                self.assertEqual(self.active(memory), [NEW])

    def test_existing_fragments_converge_on_the_next_write(self):
        """存量：同槽已有兩條現值（舊版沒收到）⇒ 下一次寫入時收成一條。"""
        for name, memory in self.backends():
            with self.subTest(name):
                # 先當成多值寫進去（不收），再把它們改成同一個單值槽位的狀態
                self.write(memory, OLD, 100.0, kind="preference")
                self.write(memory, MID, 150.0, kind="preference")
                if name == "sqlite":
                    memory._db.execute("UPDATE facts SET kind = 'state'")
                    memory._db.commit()
                else:
                    for fact in memory._facts:
                        fact.kind = "state"
                self.assertEqual(self.active(memory), sorted([MID, OLD]))
                self.write(memory, NEW, 200.0)
                self.assertEqual(self.active(memory), [NEW])

    def test_fragments_converge_onto_the_newest_even_when_the_write_is_old(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.write(memory, MID, 150.0, kind="preference")
                self.write(memory, NEW, 200.0, kind="preference")
                if name == "sqlite":
                    memory._db.execute("UPDATE facts SET kind = 'state'")
                    memory._db.commit()
                else:
                    for fact in memory._facts:
                        fact.kind = "state"
                self.write(memory, OLD, 100.0)
                self.assertEqual(self.active(memory), [NEW])

    def test_multi_valued_kinds_are_left_alone(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.write(memory, "主人はミントが好き", 100.0, kind="preference", slot="主人の好きなもの")
                self.write(memory, "主人は小説が好き", 200.0, kind="preference", slot="主人の好きなもの")
                self.assertEqual(len(self.active(memory, "主人の好きなもの")), 2)

    def test_address_slot_is_single_valued(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.write(memory, "主人のことは「船長」と呼ぶ。", 100.0, kind="rule", slot=ADDRESS_SLOT)
                self.write(memory, "主人のことは「オーナー」と呼ぶ。", 200.0, kind="rule",
                           slot=ADDRESS_SLOT)
                self.assertEqual(self.active(memory, ADDRESS_SLOT), ["主人のことは「オーナー」と呼ぶ。"])

    def test_scenes_do_not_settle_each_other(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.write(memory, OLD, 100.0, game=0)
                self.write(memory, NEW, 200.0, game=1)
                self.assertEqual(self.active(memory), sorted([NEW, OLD]))

    def test_expired_value_is_not_resurrected_or_touched(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.write(memory, OLD, 100.0, valid_to=120.0)
                self.write(memory, NEW, 200.0)
                old = self.row(memory, OLD)
                self.assertEqual(old["superseded_by"], "", "早就過期的不必再標成被取代")
                self.assertEqual(self.active(memory), [NEW])


class TestRebuildGivesTheSameAnswer(unittest.TestCase):
    def test_out_of_order_log_replays_to_the_same_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            memory = SqliteMemory(root / "facts.db", store=FactStore(root / "facts.jsonl"), seed=False)
            for text, at in ((NEW, 200.0), (OLD, 100.0), (MID, 150.0)):
                Backends.write(memory, text, at)
            before = {row["text"]: (row["active"], row["superseded_by"], row["supersedes"])
                      for row in memory.snapshot(None, entity="mira")["facts"]}
            raw = (root / "facts.jsonl").read_bytes()
            self.assertEqual(memory.rebuild(), 3)
            after = {row["text"]: (row["active"], row["superseded_by"], row["supersedes"])
                     for row in memory.snapshot(None, entity="mira")["facts"]}
            self.assertEqual(after, before)
            self.assertEqual([text for text, row in after.items() if row[0]], [NEW])
            self.assertEqual((root / "facts.jsonl").read_bytes(), raw, "重建不改正本")
            late = json.loads(raw.splitlines()[1])
            self.assertTrue(late["superseded_by"], "晚到的舊值在正本裡就寫著「已被取代」")


class FakeDistiller:
    """`apply_candidates` 需要的最小形狀（與 Distiller 同名的幾個方法）。"""

    min_conf = 0.6

    def __init__(self, memory) -> None:
        self.memory = memory
        self._inserted: dict = {}

    def _clock(self) -> float:
        return 1_700_000_000.0

    def normalize_names(self, text):
        return text

    def noise_reason(self, text):
        return ""

    def _near_duplicate(self, entity, text):
        return ""

    def _near_inserted(self, entity, text):
        return ""

    def _exists(self, entity, text):
        return False

    def _resolve_supersedes(self, entity, texts, *, game=0):
        return []


class TestDistillOutOfOrder(Backends):
    @staticmethod
    def session(at: float, game: int = 0) -> dict:
        return {"key": f"w{at}", "ended_at": at, "room": ["mira", "owner"],
                "participants": ["owner"], "game": game}

    def distill(self, memory, text, at):
        candidate = {"text": text, "layer": "pair", "confidence": 0.9, "evidence": ["t1"],
                     "kind": "state", "slot": SLOT}
        apply_candidates(FakeDistiller(memory), self.session(at), [candidate], entity="mira",
                         run_id="r", history=[], summary={"facts_added": 0, "facts_skipped": 0})

    def test_backfilled_old_window_does_not_take_the_slot(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.distill(memory, NEW, 200.0)
                self.distill(memory, OLD, 100.0)          # 補跑的舊窗口
                self.assertEqual(self.active(memory), [NEW])

    def test_in_order_windows_still_refresh(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.distill(memory, OLD, 100.0)
                self.distill(memory, NEW, 200.0)
                self.assertEqual(self.active(memory), [NEW])


class TestSlotHint(Backends):
    def test_lists_current_slot_names_of_the_same_scene(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.write(memory, OLD, 100.0)
                self.write(memory, "主人は眠そう", 110.0, slot="主人のいまの気分")
                self.write(memory, "主人は港町にいる", 120.0, slot="主人の現在地", game=1)
                self.write(memory, "主人のことは「オーナー」と呼ぶ。", 130.0, kind="rule",
                           slot=ADDRESS_SLOT)
                hint = slot_hint(memory, "mira", 0)
                self.assertIn(SLOT, hint)
                self.assertIn("主人のいまの気分", hint)
                self.assertNotIn("主人の現在地", hint, "桌上的槽位名不給現實窗口")
                self.assertNotIn(ADDRESS_SLOT, hint)
                self.assertIn("主人の現在地", slot_hint(memory, "mira", 1))

    def test_superseded_slots_are_not_listed_twice_and_empty_is_silent(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.assertEqual(slot_hint(memory, "mira", 0), "")
                self.write(memory, OLD, 100.0)
                self.write(memory, NEW, 200.0)
                self.assertEqual(slot_hint(memory, "mira", 0).count(SLOT), 1)
                self.assertEqual(slot_hint(memory, "mira", 0, limit=0), "")

    def test_hint_goes_before_the_window(self):
        prompt = "規則…\n\n会話ウィンドウ：\n"
        self.assertEqual(with_slot_hint(prompt, ""), prompt)
        out = with_slot_hint(prompt, "HINT\n\n")
        self.assertTrue(out.endswith("HINT\n\n会話ウィンドウ：\n"))
        self.assertEqual(with_slot_hint("no marker", "HINT"), "no markerHINT")


class TestBackfillTool(unittest.TestCase):
    """`agent/tools/migrations/converge_slots_20261116.py`：存量碎片的只讀盤點與收斂。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = pathlib.Path(self.tmp.name)
        base = self.data / "memory"
        memory = SqliteMemory(base / "facts.db", store=FactStore(base / "facts.jsonl"), seed=False)
        # 先當多值寫進去（不收），再改成狀態 ⇒ 同槽三條現值的存量碎片
        for text, at in ((OLD, 100.0), (NEW, 200.0), (MID, 150.0)):
            Backends.write(memory, text, at, kind="preference")
        Backends.write(memory, "主人は港町にいる", 120.0, slot="主人の現在地", game=1)
        Backends.write(memory, "主人はミントが好き", 100.0, kind="preference", slot="好きなもの")
        Backends.write(memory, "主人は小説が好き", 200.0, kind="preference", slot="好きなもの")
        memory._db.execute("UPDATE facts SET kind = 'state' WHERE slot = ?", (SLOT,))
        memory._db.commit()
        memory.close()
        self.db, self.jsonl = base / "facts.db", base / "facts.jsonl"

    def current(self) -> list[str]:
        memory = SqliteMemory(self.db, seed=False)
        try:
            return sorted(row["text"] for row in memory.snapshot(None, entity="mira")["facts"]
                          if row["slot"] == SLOT and row["active"])
        finally:
            memory.close()

    def test_scan_is_read_only_and_picks_the_latest(self):
        from agent.tools.migrations import converge_slots_20261116 as tool

        before = (self.db.read_bytes(), self.jsonl.read_bytes(), self.db.stat().st_mtime_ns)
        groups = tool.scan(self.db, now=1000.0)
        self.assertEqual(len(groups), 1, "多值的好惡、只有一條現值的槽位都不算")
        self.assertEqual(groups[0]["keep"]["text"], NEW)
        self.assertEqual(sorted(item["text"] for item in groups[0]["retire"]), sorted([OLD, MID]))
        self.assertIn(NEW, tool.report(groups))
        self.assertEqual((self.db.read_bytes(), self.jsonl.read_bytes(),
                          self.db.stat().st_mtime_ns), before)

    def test_apply_converges_is_idempotent_and_survives_rebuild(self):
        from agent.tools.migrations import converge_slots_20261116 as tool

        original = self.jsonl.read_bytes()
        first = tool.apply(self.data, now=1000.0)
        self.assertEqual((first["groups"], first["retired"]), (1, 2))
        self.assertEqual(len(first["backups"]), 2)
        self.assertEqual(self.current(), [NEW])
        after = self.jsonl.read_bytes()
        self.assertEqual(after[:len(original)], original, "正本既有行不改，只追加操作記錄")
        second = tool.apply(self.data, now=1000.0, backup=False)
        self.assertEqual((second["groups"], second["retired"]), (0, 0))
        self.assertEqual(self.jsonl.read_bytes(), after, "第二次沒有東西可收")
        memory = SqliteMemory(self.db, store=FactStore(self.jsonl), seed=False)
        self.addCleanup(memory.close)
        memory.rebuild()
        self.assertEqual(self.current(), [NEW], "重建索引之後結果一樣")


if __name__ == "__main__":
    unittest.main(verbosity=2)
