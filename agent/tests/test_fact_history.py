#!/usr/bin/env python3
"""`facts_history` 收進 `runtime/memory/fact_history.py`（review.md 2026-10-25 P1）。

判據：
  ① `memory_db_path`：SQLite 版回它的檔、Hindsight 版回本地檔、記憶體版回空字串
     （以前提煉器在記憶體版會得到字串 "None"，在工作目錄開一個叫 None 的庫檔）；
  ② 寫了查得到（新的在前、limit 有上限）；表還不存在 ⇒ 回空、不建表；
  ③ 提煉器接記憶體版時寫履歷不會在工作目錄留下 `None` 檔；
  ④ 生產原始碼只有 fact_history 寫這張表的 SQL。
"""

from __future__ import annotations

import ast
import os
import pathlib
import sqlite3
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_hindsight import HindsightMemory
from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.memory.in_memory import InMemoryMemory
from agent.brain.runtime.memory.fact_history import FactHistory, memory_db_path

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _row(key, *, at, action="insert"):
    return {"fact_key": key, "entity": "mira", "action": action, "confidence": 0.8,
            "reason": "", "evidence": ["th:t1"], "run_id": "r1", "at": at}


class TestMemoryDbPath(unittest.TestCase):
    def test_each_backend(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-hist-"))
        sqlite_memory = SqliteMemory(root / "a.db")
        hindsight = HindsightMemory(root / "b.db", host="http://127.0.0.1:9")
        self.assertEqual(memory_db_path(sqlite_memory), str(root / "a.db"))
        self.assertEqual(memory_db_path(hindsight), str(root / "b.db"))
        self.assertEqual(memory_db_path(InMemoryMemory()), "", "不是字串 'None'")
        self.assertEqual(memory_db_path(None), "")


class TestFactHistory(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp(prefix="ba-hist-")) / "facts.db"

    def test_missing_table_reads_empty_without_creating_it(self):
        sqlite3.connect(self.path).close()
        self.assertEqual(FactHistory(self.path).for_fact("x"), [])
        tables = [row[0] for row in sqlite3.connect(self.path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertNotIn("facts_history", tables)

    def test_append_then_read_newest_first(self):
        history = FactHistory(self.path)
        history.append([_row("主人喜欢薄荷", at=1.0), _row("主人喜欢薄荷", at=2.0, action="skip"),
                        _row("别的", at=3.0)], model="m")
        rows = history.for_fact("主人喜欢薄荷")
        self.assertEqual([row["action"] for row in rows], ["skip", "insert"])
        self.assertEqual(rows[0]["model"], "m")
        self.assertEqual(rows[0]["evidence"], "th:t1")
        self.assertEqual(len(history.for_fact("主人喜欢薄荷", limit=1)), 1)
        self.assertEqual(FactHistory("").for_fact("主人喜欢薄荷"), [])


class TestDistillerWithoutDbFile(unittest.TestCase):
    def test_no_stray_none_file(self):
        from agent.brain.runtime.distill import Distiller
        from agent.brain.runtime.rawlog import RawLog
        workdir = tempfile.mkdtemp(prefix="ba-cwd-")
        previous = os.getcwd()
        os.chdir(workdir)
        self.addCleanup(os.chdir, previous)
        distiller = Distiller(memory=InMemoryMemory(), raw_log=RawLog(pathlib.Path(workdir) / "raw"),
                              entities=("mira",), api_key="")
        distiller._history([_row("x", at=1.0)])                # noqa: SLF001 - 驗這條路徑
        self.assertFalse(pathlib.Path(workdir, "None").exists(), "不該開出叫 None 的庫檔")


class TestOnlyOneOwner(unittest.TestCase):
    def test_no_other_module_writes_sql_against_the_table(self):
        hits = []
        for folder in ("brain", "adapter", "server"):
            for path in (ROOT / "agent" / folder).rglob("*.py"):
                if path.name == "fact_history.py":
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and "facts_history" in node.value
                            and any(word in node.value.upper()
                                    for word in ("SELECT", "INSERT", "CREATE TABLE"))):
                        hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(hits, [], "facts_history 的 SQL 只准寫在 runtime/memory/fact_history.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
