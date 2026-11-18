#!/usr/bin/env python3
"""`window_summaries` 只有一個主人（`runtime/memory/window_store.py`，2026-10-25 第 3 階段）。

判據：
  ① 寫了讀得到：recent（新的在前）、overlapping（前後放寬）、items（給向量索引）；
  ② 表還不存在（還沒提煉過）⇒ 讀回空、不建表、不拋例外；
  ③ 生產原始碼裡只有 window_store 寫 SQL 碰這張表（其他人一律走它）。
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.memory.window_store import WindowStore

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _record(window_id, *, start, end, thread="th_a-1", entity="mira"):
    return {"window_id": window_id, "entity": entity, "thread": thread, "started_at": start,
            "ended_at": end, "turns": 8, "topic": f"topic {window_id}",
            "summary": f"summary {window_id}", "people": [], "open_loops": [], "run_id": "r",
            "at": end}


class TestWindowStore(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp(prefix="ba-win-")) / "facts.db"
        self.store = WindowStore(self.path)

    def test_missing_table_reads_empty_and_does_not_create_it(self):
        sqlite3.connect(self.path).close()
        self.assertEqual(self.store.recent("mira"), [])
        self.assertEqual(self.store.overlapping("mira", 0, 10), [])
        self.assertEqual(self.store.items("mira"), [])
        tables = [row[0] for row in sqlite3.connect(self.path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertNotIn("window_summaries", tables, "讀路徑不建表")
        self.assertEqual(WindowStore("").recent("mira"), [])

    def test_upsert_then_read(self):
        self.store.upsert(_record("w1", start=100, end=200))
        self.store.upsert(_record("w2", start=1000, end=1100))
        self.store.upsert(_record("w9", start=100, end=200, entity="aster"))
        self.assertEqual([row["window_id"] for row in self.store.recent("mira")], ["w2", "w1"])
        self.assertEqual([row["window_id"] for row in self.store.recent("mira", limit=1)], ["w2"])
        # 前後放寬 60 秒：260 還碰得到 w1（ended 200），1000 以前碰不到 w2
        self.assertEqual([row["window_id"] for row in self.store.overlapping("mira", 250, 260)],
                         ["w1"])
        self.assertEqual(self.store.items("mira"),
                         [("w1", "topic w1 summary w1"), ("w2", "topic w2 summary w2")])
        self.store.upsert({**_record("w1", start=100, end=200), "summary": "改過了"})
        self.assertEqual(self.store.recent("mira")[1]["summary"], "改過了", "同 id 覆蓋")


class TestOnlyOneOwner(unittest.TestCase):
    def test_no_other_module_writes_sql_against_the_table(self):
        hits = []
        for folder in ("brain", "adapter", "server"):
            for path in (ROOT / "agent" / folder).rglob("*.py"):
                if path.name == "window_store.py":
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and "window_summaries" in node.value
                            and any(word in node.value.upper()
                                    for word in ("SELECT", "INSERT", "UPDATE", "DELETE",
                                                 "CREATE TABLE"))):
                        hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(hits, [], "window_summaries 的 SQL 只准寫在 runtime/memory/window_store.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
