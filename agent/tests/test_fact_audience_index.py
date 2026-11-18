#!/usr/bin/env python3
"""`fact_audience` 索引（owner 2026-11-16「拆 fact_audience 表让焦点窗快速校准」）。

三件要钉住：
1. 写入路径同步：`SqliteMemory.add()` 之后索引里有受眾列（`audience=None` 的世界事实不进来）。
2. 查询等价：走索引与走 `audience LIKE` 的焦點窗结果**完全一致**（含 principal 那条）。
3. 回填可重跑：跑两次列数不变（INSERT OR IGNORE）；回填后查詢自動切到索引。
4. **「完整」標記**（審核 R2，2026-11-17）：表裡有行≠回填完；部分回填時焦點窗必須走 LIKE、結果不能少舊事實。
   空庫開庫即標記；舊庫開庫當場補跑；回填中途停下不置位；兩邊的版本常數一致。
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "agent" / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent.adapter import fact_audience_index  # noqa: E402
from agent.adapter.memory_sqlite import SqliteMemory  # noqa: E402
from agent.brain.runtime.memory import attention_field  # noqa: E402

PAIR = frozenset({"owner", "mira"})


class FactAudienceIndexCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="ba-fai-")
        self.addCleanup(self.dir.cleanup)
        self.path = pathlib.Path(self.dir.name) / "facts.db"
        self.memory = SqliteMemory(self.path, seed=False)

    def test_add_indexes_audience_and_principal(self):
        self.memory.add("主人は辛いものが苦手", layer="pair", entity="mira",
                        audience=PAIR, principal="owner", source="distill")
        db = sqlite3.connect(str(self.path))
        try:
            rows = db.execute("SELECT member, kind FROM fact_audience ORDER BY kind, member").fetchall()
        finally:
            db.close()
        self.assertIn(("owner", "audience"), rows)
        self.assertIn(("mira", "audience"), rows)
        self.assertIn(("owner", "principal"), rows)

    def test_world_fact_has_no_audience_rows(self):
        self.memory.add("今日は雨だった", layer="self", entity="mira", source="activity_public")
        db = sqlite3.connect(str(self.path))
        try:
            count = db.execute("SELECT COUNT(*) FROM fact_audience").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 0, "audience=None（世界事實）不進索引")

    def test_index_and_like_paths_agree(self):
        self.memory.add("主人は辛いものが苦手", layer="pair", entity="mira",
                        audience=PAIR, principal="owner", source="distill")
        self.memory.add("アスターは星が好き", layer="private", entity="mira",
                        audience=frozenset({"owner", "aster"}), principal="aster", source="distill")
        self.memory.add("誰でも知ってること", layer="private", entity="mira",
                        audience=None, source="distill")

        os.environ["BLUEAGENT_FOCUS_INDEX"] = "1"
        fast = [row["text"] for row in attention_field.focus_rows(self.path, "mira", {"owner"})]
        os.environ["BLUEAGENT_FOCUS_INDEX"] = "0"
        slow = [row["text"] for row in attention_field.focus_rows(self.path, "mira", {"owner"})]
        os.environ.pop("BLUEAGENT_FOCUS_INDEX", None)
        self.assertEqual(fast, slow)
        self.assertIn("主人は辛いものが苦手", fast)

    def test_backfill_is_rerunnable(self):
        for index in range(5):
            self.memory.add(f"事実その{index}", layer="pair", entity="mira",
                            audience=PAIR, principal="owner", source="distill")
        db = sqlite3.connect(str(self.path))
        try:
            first = fact_audience_index.backfill(db)
            second = fact_audience_index.backfill(db)
            total = fact_audience_index.count(db)
        finally:
            db.close()
        self.assertEqual(first["written"], second["written"], "第二次回填寫入列數不變（都是重複）")
        self.assertEqual(first["rows"], total, "總列數穩定")

    def test_focus_rows_use_index_when_present(self):
        self.memory.add("主人の好み", layer="pair", entity="nori",
                        audience=PAIR, principal="owner", source="distill")
        db = sqlite3.connect(str(self.path))
        try:
            self.assertTrue(fact_audience_index.has_table(db))
            self.assertTrue(attention_field._index_ready(db))
        finally:
            db.close()



class BackfillCompletenessCase(unittest.TestCase):
    """審核 R2 的最小複現：只有較新的一條在 `fact_audience` 裡（升級後、回填前有新事實寫入）。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="ba-fai-r2-")
        self.addCleanup(self.dir.cleanup)
        self.path = pathlib.Path(self.dir.name) / "facts.db"
        memory = SqliteMemory(self.path, seed=False)
        memory.add("古い関係記憶", layer="pair", entity="mira", audience=PAIR,
                   principal="owner", source="distill")
        memory.add("新しい関係記憶", layer="pair", entity="mira", audience=PAIR,
                   principal="owner", source="distill")
        attention_field.forget_connections()
        self.addCleanup(attention_field.forget_connections)

    def _db(self):
        db = sqlite3.connect(str(self.path))
        self.addCleanup(db.close)
        return db

    def _simulate_partial(self):
        """回到「升級後、回填前」：清標記，舊事實的投影拿掉，只留新那條。"""
        db = self._db()
        db.execute("DELETE FROM fact_audience_meta")
        old_id = db.execute("SELECT id FROM facts WHERE text='古い関係記憶'").fetchone()[0]
        db.execute("DELETE FROM fact_audience WHERE fact_id=?", (old_id,))
        db.commit()
        attention_field.forget_connections()
        return db

    def focus(self):
        return sorted(row["text"] for row in attention_field.focus_rows(self.path, "mira", {"owner"}))

    def test_fresh_db_is_marked_complete_on_open(self):
        self.assertTrue(fact_audience_index.is_complete(self._db()))
        self.assertTrue(attention_field._index_ready(self._db()))

    def test_partial_backfill_falls_back_and_keeps_old_facts(self):
        db = self._simulate_partial()
        self.assertEqual(fact_audience_index.count(db) > 0, True, "表裡還有行（以前就會被當成完整）")
        self.assertFalse(attention_field._index_ready(db))
        self.assertEqual(self.focus(), ["古い関係記憶", "新しい関係記憶"], "LIKE 路徑：舊事實還在")

    def test_reopening_backfills_and_restores_the_index_path(self):
        self._simulate_partial()
        SqliteMemory(self.path, seed=False)           # 開庫 ⇒ ensure_ready 補跑
        attention_field.forget_connections()
        self.assertTrue(attention_field._index_ready(self._db()))
        self.assertEqual(self.focus(), ["古い関係記憶", "新しい関係記憶"], "索引路徑：補齊之後一樣完整")

    def test_interrupted_backfill_does_not_mark_complete(self):
        db = self._simulate_partial()
        result = fact_audience_index.backfill(db, batch=1, limit_rows=1)
        self.assertFalse(result["complete"])
        self.assertFalse(fact_audience_index.is_complete(db))
        full = fact_audience_index.backfill(db)
        self.assertTrue(full["complete"])
        self.assertTrue(fact_audience_index.is_complete(db))

    def test_empty_single_partial_complete_all_agree_with_like(self):
        def both():
            attention_field.forget_connections()
            os.environ["BLUEAGENT_FOCUS_INDEX"] = "1"
            try:
                fast = self.focus()
            finally:
                os.environ["BLUEAGENT_FOCUS_INDEX"] = "0"
            try:
                slow = self.focus()
            finally:
                os.environ.pop("BLUEAGENT_FOCUS_INDEX", None)
            return fast, slow

        db = self._db()
        for label, prepare in (
                ("empty", lambda: (db.execute("DELETE FROM fact_audience_meta"),
                                   db.execute("DELETE FROM fact_audience"), db.commit())),
                ("partial", self._simulate_partial),
                ("complete", lambda: fact_audience_index.backfill(db))):
            with self.subTest(state=label):
                prepare()
                fast, slow = both()
                self.assertEqual(fast, slow)
                self.assertEqual(fast, ["古い関係記憶", "新しい関係記憶"])

    def test_version_constant_matches_runtime_copy(self):
        self.assertEqual(attention_field._BACKFILL_VERSION, fact_audience_index.BACKFILL_VERSION)


if __name__ == "__main__":
    unittest.main()
