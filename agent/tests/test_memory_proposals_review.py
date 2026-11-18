#!/usr/bin/env python3
"""記憶三件的審查返修（2026-11-16，接 `11dbc9c`…`660bd64`）。

判據：
  ① 自述（self-model）只收**現實**、仍有效、房間只有她或只有她＋主人的事實：
     桌上的身份、她和アスター的私事、世界事實、已過期的都不進；
  ② `fact_audience` 投影跟著 `facts` 刪：`clear()`／`rebuild()`／按 id 刪之後號碼被重用，
     焦點窗不會把別人的事實掛到主人身上；
  ③ 使用量回饋在真的一輪裡會寫（召回清單在 `env.meta`，管線 ctx 沒有 `turn`），
     而且只記這個角色、這個場景的那一條。
"""

from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.memory.fact_store import FactStore
from agent.brain.runtime.inner import self_model
from agent.brain.runtime.memory import attention_field, usage_feedback


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-mem-review-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.db_path = self.root / "facts.db"
        self.addCleanup(attention_field.forget_connections)

    def memory(self, *, store: bool = False) -> SqliteMemory:
        backing = FactStore(self.root / "facts.jsonl") if store else None
        memory = SqliteMemory(self.db_path, store=backing)
        self.addCleanup(lambda: getattr(memory, "close", lambda: None)())
        return memory

    def column(self, sql: str, *params) -> list:
        db = sqlite3.connect(self.db_path)
        try:
            return [row[0] for row in db.execute(sql, params).fetchall()]
        finally:
            db.close()


class TestSelfModelScope(Base):
    def test_only_her_reality_and_her_room_with_the_owner(self):
        memory = self.memory()
        mine = {"layer": "self", "audience": {"mira"}, "entity": "mira", "kind": "identity"}
        memory.add("わたしは実験ノートを通し番号で書く", **mine)
        memory.add("わたしは騎士団の団長だ", **mine, game=1)
        memory.add("主人のことは「オーナー」と呼ぶ", layer="pair", audience={"owner", "mira"},
                   entity="mira", principal="owner", kind="称呼")
        memory.add("アスターとだけの秘密：転校を考えている", layer="pair", audience={"aster", "mira"},
                   entity="mira", principal="aster", kind="state")
        memory.add("三人で決めた合言葉は青", layer="circle", audience={"owner", "aster", "mira"},
                   entity="mira", principal="owner", kind="rule")
        memory.add("水は百度で沸騰する", layer="world", audience=None, entity="mira", kind="state")
        memory.add("今日は風邪で寝ている", **{**mine, "kind": "state"},
                   valid_to=time.time() - 3600)
        result = self_model.rebuild(self.root, "mira", db_path=self.db_path)
        text = "\n".join(result["stable"] + result["recent"])
        self.assertIn("通し番号", text, "房間只有她 ⇒ 收")
        self.assertIn("オーナー", text, "她＋主人 ⇒ 收")
        for leaked, why in (("騎士団", "桌上的身份不是她"), ("転校", "她和アスター的私事"),
                            ("合言葉", "有第三人的房間"), ("沸騰", "世界事實不是自述"),
                            ("風邪", "已過期")):
            self.assertNotIn(leaked, text, why)

    def test_eligible_rule(self):
        self.assertTrue(self_model.eligible({"mira"}, "mira"))
        self.assertTrue(self_model.eligible({"mira", "acct_owner"}, "mira"))
        self.assertFalse(self_model.eligible({"mira", "aster"}, "mira"))
        self.assertFalse(self_model.eligible(None, "mira"))
        self.assertFalse(self_model.eligible(set(), "mira"))


class TestAudienceIndexFollowsDeletes(Base):
    def focus_texts(self, who: str) -> list[str]:
        attention_field.forget_connections()
        return [row["text"] for row in attention_field.focus_rows(self.db_path, "mira", {who})]

    def test_clear_does_not_leave_stale_members(self):
        memory = self.memory()
        memory.add("主人はラーメンが好き", layer="pair", audience={"owner", "mira"},
                   entity="mira", principal="owner")
        memory.clear()
        memory.add("ノリはゲームが好き", layer="pair", audience={"nori", "mira"},
                   entity="mira", principal="nori")
        self.assertEqual(self.focus_texts("owner"), [], "1 號重用了，舊的 owner 列不能掛上 Nori 的事")
        self.assertEqual(self.focus_texts("nori"), ["ノリはゲームが好き"])
        self.assertEqual(sorted(self.column("SELECT member FROM fact_audience")),
                         ["mira", "nori", "nori"])

    def test_rebuild_after_a_deleted_row_keeps_the_index_true(self):
        memory = self.memory(store=True)
        memory.add("主人は猫派", layer="pair", audience={"owner", "mira"},
                   entity="mira", principal="owner")
        memory.add("アスターは数学が得意", layer="pair", audience={"aster", "mira"},
                   entity="mira", principal="aster")
        # 索引被手動清理過（prune_memory 那種按 id 刪）⇒ 重建時號碼會挪位
        db = sqlite3.connect(self.db_path)
        db.execute("DELETE FROM facts WHERE id = 1")
        db.commit()
        db.close()
        self.assertEqual(self.column("SELECT DISTINCT fact_id FROM fact_audience"), [2])
        memory.rebuild()
        self.assertEqual(self.focus_texts("owner"), ["主人は猫派"])
        self.assertEqual(self.focus_texts("aster"), ["アスターは数学が得意"])


class TestUsageFeedbackScope(Base):
    def seed(self) -> SqliteMemory:
        memory = self.memory()
        for entity in ("mira", "aster"):
            memory.add("主人は辛いものが苦手だ", layer="pair", audience={"owner", entity},
                       entity=entity, principal="owner")
        # 同一倉同一句只有一條（UNIQUE(norm, entity)）⇒ 桌上那條放在 nori 的倉
        memory.add("主人は辛いものが苦手だ", layer="pair", audience={"owner", "nori"},
                   entity="nori", principal="owner", game=1)
        return memory

    def used(self) -> list[tuple]:
        db = sqlite3.connect(self.db_path)
        try:
            return db.execute("SELECT entity, game, utility FROM facts ORDER BY entity, game").fetchall()
        finally:
            db.close()

    def test_only_her_row_is_marked(self):
        memory = self.seed()
        ctx = SimpleNamespace(ports=SimpleNamespace(memory=memory))   # AppPorts 形
        written = usage_feedback.mark(ctx, "辛いものは苦手だったよね", recall=["主人は辛いものが苦手だ"],
                                      entity="mira", game=0)
        self.assertEqual(written, 1)
        self.assertEqual(self.used(), [("aster", 0, 0), ("mira", 0, 1), ("nori", 1, 0)],
                         "只記米拉那一條（A.9 各自學習）")

    def test_reality_turn_does_not_mark_the_table_row(self):
        memory = self.seed()
        ctx = SimpleNamespace(ports=SimpleNamespace(memory=memory))
        said, recall = "辛いものは苦手だったよね", ["主人は辛いものが苦手だ"]
        usage_feedback.mark(ctx, said, recall=recall, entity="nori", game=0)
        self.assertIn(("nori", 1, 0), self.used(), "現實回合不記桌上那條")
        usage_feedback.mark(ctx, said, recall=recall, entity="nori", game=1)
        self.assertIn(("nori", 1, 1), self.used())

    def test_note_usage_without_scope_keeps_old_behaviour(self):
        memory = self.seed()
        self.assertEqual(memory.note_usage(["主人は辛いものが苦手だ"], used=True), 3)


class TestUsageFeedbackInARealTurn(unittest.TestCase):
    """生產管線：召回清單在 `env.meta`、管線 ctx 沒有 `turn`——舊版在這裡每輪都靜默寫 0 條。"""

    def test_a_turn_marks_the_recalled_fact(self):
        import os
        import demo_common
        from agent.brain.compose.state import AppState

        os.environ.setdefault("BLUEAGENT_BRAIN", "mock")
        data_dir = tempfile.mkdtemp(prefix="ba-usage-turn-")
        app = demo_common.build(brain_delay=0.0, data_dir=data_dir, step_duration=0.02,
                                life_pause=0.005, seed=False)
        local = getattr(app.memory, "local", app.memory)
        local.add("主人は辛いものが苦手だ", layer="pair", audience={"owner", "aster"},
                  entity="aster", principal="owner")
        calls = []
        for node in {id(app.memory): app.memory, id(local): local}.values():
            node.note_usage = lambda texts, **scope: calls.append((list(texts), scope))

        def run_turn(_request):
            yield "delta", {"text": "辛いものは苦手だったよね。"}
            yield "done", {"text": "辛いものは苦手だったよね。"}

        app.brain.run_turn = run_turn
        result = AppState(app).turns.run("辛いもの、覚えてる？", entity="aster", principal="owner")
        self.assertEqual(demo_common.done_text(result), "辛いものは苦手だったよね。")
        self.assertEqual(calls, [(["主人は辛いものが苦手だ"], {"used": True, "entity": "aster", "game": 0})])


if __name__ == "__main__":
    unittest.main(verbosity=2)
