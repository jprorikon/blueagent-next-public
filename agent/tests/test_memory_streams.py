#!/usr/bin/env python3
"""記憶分流的第一步：`stream`／`topic` 兩個欄位（owner 2026-11-03）。

判據：
  ① 來源 → 流：生活流的來源一律 `life`，**其餘（含未知）都是 `dialogue`**（寧可多記對話）；
  ② 寫入時就把流落庫（不是讀的時候才猜），沒指定的按來源推導；
  ③ 主題只給生活流填；能對上 preset 標題就是穩定 key，對不上就用原標題；
  ④ 三個後端（sqlite／記憶體／hindsight）**介面一致**——少一個參數，提煉器整批 TypeError 被靜默吞掉。
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent" / "tools"))

from agent.brain.memory.facts import (STREAM_DIALOGUE, STREAM_LIFE,  # noqa: E402
                                         stream_for_source)


class TestStreamMapping(unittest.TestCase):
    def test_life_sources(self):
        for source in ("self_note", "activity_public", "activity_summary", "reflection"):
            with self.subTest(source=source):
                self.assertEqual(stream_for_source(source), STREAM_LIFE)

    def test_everything_else_is_dialogue(self):
        for source in ("distill", "explicit", "rule", "manual", "seed", "", "unknown-source"):
            with self.subTest(source=source):
                self.assertEqual(stream_for_source(source), STREAM_DIALOGUE)


class TestSqlitePersistsStream(unittest.TestCase):
    def setUp(self):
        from agent.adapter.memory_sqlite import SqliteMemory
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-streams-"))
        self.db_path = self.root / "facts.db"
        self.memory = SqliteMemory(self.db_path, entity_ids=("mira", "aster", "nori"))

    def tearDown(self):
        self.memory.close()

    def test_stream_column_is_written_from_source(self):
        self.memory.add("今日のノートを読み返した。", source="self_note", entity="mira",
                        layer="self", topic="notes-reread")
        self.memory.add("主人は来週試験があると言った。", source="distill", entity="mira")
        db = sqlite3.connect(str(self.db_path))
        rows = dict(db.execute("SELECT source, stream FROM facts").fetchall())
        topic = db.execute("SELECT topic FROM facts WHERE source = 'self_note'").fetchone()[0]
        db.close()
        self.assertEqual(rows["self_note"], STREAM_LIFE)
        self.assertEqual(rows["distill"], STREAM_DIALOGUE)
        self.assertEqual(topic, "notes-reread")

    def test_explicit_stream_wins(self):
        fact = self.memory.add("明示指定", source="self_note", entity="mira",
                               stream=STREAM_DIALOGUE)
        self.assertEqual(fact.stream, STREAM_DIALOGUE)

    def test_stored_row_reads_back_with_stream(self):
        self.memory.add("想起したこと", source="activity_summary", entity="aster", layer="self")
        row = self.memory._db.execute("SELECT * FROM facts LIMIT 1").fetchone()
        fact = self.memory._row_to_fact(row)
        self.assertEqual(fact.stream, STREAM_LIFE)
        self.assertEqual(fact.to_dict()["stream"], STREAM_LIFE)


class TestMemoryBackendParity(unittest.TestCase):
    """三個後端的 `add()` 都要吃得下 `stream`／`topic`（否則提煉器整批 TypeError 被吞）。"""

    def test_in_memory_accepts_the_same_kwargs(self):
        from agent.brain.memory.in_memory import InMemoryMemory
        memory = InMemoryMemory(entity_ids=("mira",))
        fact = memory.add("她的筆記", source="self_note", entity="mira",
                          layer="self", stream=STREAM_LIFE, topic="notes-reread")
        self.assertEqual((fact.stream, fact.topic), (STREAM_LIFE, "notes-reread"))


class TestTopicOf(unittest.TestCase):
    def setUp(self):
        import backfill_streams
        self.tool = backfill_streams
        self.titles = self.tool._preset_keys()

    def test_preset_title_becomes_stable_key(self):
        title = next(iter(self.titles))
        key = self.titles[title]
        self.assertEqual(self.tool.topic_of(f"ミラ「{title}」2/4 何か：本文", self.titles), key)

    def test_unknown_title_falls_back_to_title(self):
        self.assertEqual(self.tool.topic_of("ミラ「誰も知らない活動」1/2 x", self.titles),
                         "誰も知らない活動")

    def test_no_title_means_no_topic(self):
        self.assertEqual(self.tool.topic_of("タイトルの無い文", self.titles), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
