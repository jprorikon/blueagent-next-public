#!/usr/bin/env python3
"""時域有效性（第一步）：事實帶「事件時間／有效期／被取代」——不再把過期的事當現在。

owner 2026-10-23：「Hindsight 的雙時域（入庫時間 vs 表達時間）」——我們現在本地做。

判據：
  ① `valid_to` 過了 ⇒ **不進被動注入**（今晚的約定，明天不會再掛在嘴邊）；
  ② 問句帶「之前／上次／那天」⇒ 允許召回已過期的事（as-of 查詢的第一層）；
  ③ 被新值取代（`superseded_by`）⇒ 當下不成立，但資料不刪（可回溯「以前是什麼」）；
  ④ `valid_at(t)` 能回答「那個時間點成立嗎」；
  ⑤ 舊庫沒有這幾欄 ⇒ 自動補欄，不影響既有資料（預設 0＝未指定）。
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory


class TestTemporalFacts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = SqliteMemory(pathlib.Path(self.tmp.name) / "facts.db", seed=False,
                                   entity_ids=("mira",))

    def add(self, text, **kwargs):
        return self.memory.add(text, layer="pair", audience={"mira", "owner"},
                               principal="owner", entity="mira", source="test", **kwargs)

    def test_expired_event_is_not_in_active_set(self):
        now = time.time()
        self.add("今晚一起準備角色卡", kind="event", valid_from=now - 7200,
                 valid_to=now - 3600)
        detail = self.memory.recall_detail(_scope(), "角色卡", participants={"mira", "owner"})
        self.assertEqual([fact.text for fact in detail], [], "過期事件不該進被動注入")

    def test_active_event_is_recalled(self):
        now = time.time()
        self.add("今晚一起準備角色卡", kind="event", valid_from=now - 600,
                 valid_to=now + 3600)
        detail = self.memory.recall_detail(_scope(), "角色卡", participants={"mira", "owner"})
        self.assertTrue(any("角色卡" in fact.text for fact in detail))

    def test_superseded_state_is_inactive_but_kept(self):
        first = self.add("主人正在讀的書是《佐々木とピーちゃん》", kind="state")
        self.add("主人正在讀的書是《另一本》", kind="state", superseded_by="")
        self.memory._db.execute(
            "UPDATE facts SET superseded_by = 'new' WHERE text = ?",
            ("主人正在讀的書是《佐々木とピーちゃん》",))
        self.memory._db.commit()
        facts = [self.memory._row_to_fact(row) for row in
                 self.memory._db.execute("SELECT * FROM facts").fetchall()]
        old = [fact for fact in facts if "佐々木" in fact.text][0]
        self.assertFalse(old.is_active(), "被取代的那條當下不成立")
        self.assertTrue(old.valid_at(time.time()), "as-of 查詢要看得到它（資料沒刪）")

    def test_preference_without_validity_stays_active(self):
        self.add("主人喜歡ほうじ茶", kind="preference")
        detail = self.memory.recall_detail(_scope(), "ほうじ茶", participants={"mira", "owner"})
        self.assertTrue(any("ほうじ茶" in fact.text for fact in detail))

    def test_old_database_gets_columns(self):
        # 模擬舊庫：只有基本欄位
        import sqlite3
        path = pathlib.Path(self.tmp.name) / "legacy.db"
        db = sqlite3.connect(str(path))
        db.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, text TEXT, norm TEXT UNIQUE,"
                   " layer TEXT, audience TEXT, label TEXT, principal TEXT, entity TEXT,"
                   " consensus INTEGER, self_owned INTEGER, source TEXT, created_at REAL,"
                   " shingles TEXT)")
        db.execute("INSERT INTO facts (text, norm, layer, audience, label, principal, entity,"
                   " consensus, self_owned, source, created_at, shingles)"
                   " VALUES ('舊資料','舊資料','pair','mira,owner','','owner','mira',0,0,'x',?, '')",
                   (time.time(),))
        db.commit()
        db.close()
        migrated = SqliteMemory(path, seed=False)
        fact = migrated._row_to_fact(migrated._db.execute("SELECT * FROM facts").fetchone())
        self.assertEqual(fact.kind, "")
        self.assertTrue(fact.is_active(), "舊資料沒有有效期 ⇒ 視為仍有效")
        # 2026-11-03 分流：舊庫要補上 stream 欄、而且分流索引在補欄之後建得起來
        self.assertEqual(fact.stream, "")
        indexes = {row[0] for row in migrated._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("facts_stream", indexes)

    def test_fresh_database_has_stream_index(self):
        fresh = SqliteMemory(pathlib.Path(self.tmp.name) / "fresh.db", seed=False)
        indexes = {row[0] for row in fresh._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("facts_stream", indexes)


class TestEventTTL(unittest.TestCase):
    def test_event_gets_default_three_days(self):
        from agent.brain.runtime.distill import event_valid_to
        at = 1_700_000_000.0
        self.assertAlmostEqual(event_valid_to("event", 0, at), at + 3 * 86400.0)   # owner 2026-11-16：1 → 3 天
        self.assertAlmostEqual(event_valid_to("event", 5, at), at + 5 * 86400.0)

    def test_non_event_never_expires(self):
        from agent.brain.runtime.distill import event_valid_to
        self.assertEqual(event_valid_to("preference", 5, 1_700_000_000.0), 0.0)
        self.assertEqual(event_valid_to("state", 0, 1_700_000_000.0), 0.0)


def _scope():
    from agent.brain.domain.thread import MemoryScope
    return MemoryScope(principal="owner", entity="mira", scene="chat", thread_id="t")


if __name__ == "__main__":
    unittest.main(verbosity=2)
