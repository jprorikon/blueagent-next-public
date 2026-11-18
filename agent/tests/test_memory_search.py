#!/usr/bin/env python3
"""`memory.search`（P3）：她能在回合裡自己翻過去，且**披露規則不能鬆**。

判據：
  ① 三層都查得到：事實／窗口摘要／原文；
  ② 披露子集：房間裡有「不在場的人」⇒ 那一筆不回（事實、窗口、原文都一樣）；
  ③ 工具面：`memory.search` 對 guest 也開放（chat），但結果已被 ② 收窄；
  ④ 查不到就回空（不塞底噪）。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.domain.thread import ThreadKey, TurnRecord
from agent.brain.runtime.memory.memory_search import MemorySearch
from agent.brain.runtime.rawlog import RawLog
from agent.brain.runtime.tools import MemorySearchTool, ToolContext


class FakeThread:
    def __init__(self, entity, thread_id, participants, turns):
        self.key = ThreadKey(scope="owner", entity=entity, scene="chat", purpose="talk")
        self.id = thread_id
        self.participants = set(participants)
        self.segment = list(turns)


def turn(turn_id, text, *, at, role="user"):
    return TurnRecord(turn_id=turn_id, role=role, text=text, source="app", at=at,
                      speaker="owner" if role == "user" else "mira")


class SearchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.memory = SqliteMemory(self.root / "facts.db", seed=False)
        self.raw = RawLog(self.root / "raw")
        now = time.time() - 3600
        self.raw.sync_thread(FakeThread("mira", "th-1", {"owner"}, [
            turn("t1", "主人在读佐々木とピーちゃん，读到第六卷。", at=now),
            turn("t2", "へえ、小説？私も読みたい。", at=now + 20, role="assistant"),
        ]))
        self.raw.sync_thread(FakeThread("mira", "th-2", {"bob"}, [
            turn("t1", "Bob 说他下个月要搬家，别告诉别人。", at=now + 40),
        ]))
        self.memory.add("主人在读《佐々木とピーちゃん》，读到第六卷。", layer="pair",
                        audience={"mira", "owner"}, entity="mira")
        self.memory.add("Bob 下个月要搬家。", layer="private",
                        audience={"mira", "bob"}, entity="mira")
        # 窗口摘要（提煉產物）
        db = sqlite3.connect(self.root / "facts.db")
        db.executescript("""CREATE TABLE IF NOT EXISTS window_summaries (
            window_id TEXT PRIMARY KEY, entity TEXT, thread TEXT, started_at REAL,
            ended_at REAL, turns INTEGER, topic TEXT, summary TEXT, people TEXT,
            open_loops TEXT, run_id TEXT, at REAL);""")
        db.execute("INSERT INTO window_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("w1", "mira", "th-1", now, now + 60, 12, "在讀的小說",
                    "主人提到在讀佐々木とピーちゃん，ミラ說也想讀。",
                    json.dumps(["mira", "owner"], ensure_ascii=False), "[]", "r1", now))
        db.execute("INSERT INTO window_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("w2", "mira", "th-2", now, now + 90, 10, "Bob 搬家",
                    "Bob 說下個月要搬家，要求保密。",
                    json.dumps(["mira", "bob"], ensure_ascii=False), "[]", "r1", now))
        db.commit()
        db.close()
        self.search = MemorySearch(memory=self.memory, raw_log=self.raw,
                                   db_path=self.root / "facts.db")

    def test_searches_all_three_layers(self):
        out = self.search.search(entity="mira", present={"mira", "owner"},
                                 query="小説 佐々木", limit=5)
        self.assertTrue(out["ok"])
        self.assertTrue(out["facts"], out)
        self.assertTrue(out["windows"], out)
        self.assertTrue(out["turns"], out)

    def test_disclosure_subset_blocks_other_rooms(self):
        out = self.search.search(entity="mira", present={"mira", "owner"},
                                 query="搬家 Bob", limit=10)
        self.assertEqual(out["hits"], 0, out)     # 三層都不該回（query 是回顯，不算內容）
        self.assertFalse([item for item in out["facts"] if "搬家" in item["text"]], out)
        self.assertFalse([item for item in out["windows"] if "搬家" in item["topic"]], out)
        self.assertFalse([item for item in out["turns"] if "搬家" in item["text"]], out)

    def test_bob_present_sees_his_own_talk(self):
        out = self.search.search(entity="mira", present={"mira", "bob"},
                                 query="搬家", limit=10)
        self.assertTrue(any("搬家" in item["text"] for item in out["turns"]), out)

    def test_empty_result_is_empty_not_noise(self):
        out = self.search.search(entity="mira", present={"mira", "owner"},
                                 query="宇宙飞船发动机", limit=5)
        self.assertEqual(out["hits"], 0, out)

    def test_tool_wraps_search_and_is_open_to_guests(self):
        tool = MemorySearchTool(self.search)
        ctx = ToolContext(entity="mira", principal="owner", thread_id="th-1",
                          source="app", voice=None)
        result = tool.run(ctx, {"query": "小説", "limit": 3})
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.data["hits"] >= 1)

    def test_recent_window_outranks_old_one(self):
        """同樣命中，昨天那段應該排在一個月前那段前面（時間衰減）。"""
        import time as _time
        now = _time.time()
        db = sqlite3.connect(self.root / "facts.db")
        db.execute("INSERT OR REPLACE INTO window_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("w_new", "mira", "th-1", now - 3600, now - 3000, 10, "在讀的小說 最近",
                    "主人提到在讀佐々木とピーちゃん，最近這幾天聊到第六卷。",
                    json.dumps(["mira", "owner"], ensure_ascii=False), "[]", "r2", now))
        db.execute("INSERT OR REPLACE INTO window_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("w_old", "mira", "th-1", now - 40 * 86400, now - 40 * 86400 + 600, 10,
                    "在讀的小說 很久以前", "主人提到在讀佐々木とピーちゃん，很久以前聊到第六卷。",
                    json.dumps(["mira", "owner"], ensure_ascii=False), "[]", "r2", now))
        db.commit()
        db.close()
        out = self.search.search(entity="mira", present={"mira", "owner"},
                                 query="佐々木 第六卷", limit=5, layers=("window",))
        ids = [item["window_id"] for item in out["windows"]]
        self.assertIn("w_new", ids)
        self.assertLess(ids.index("w_new"), ids.index("w_old"), ids)
        self.assertGreater(out["windows"][0]["score"], 0)


class TestBootstrapWiring(unittest.TestCase):
    """工具真的接上線了（不是只有類別存在）。"""

    def test_memory_search_and_weather_are_registered(self):
        root = tempfile.mkdtemp(prefix="ba-wire-")
        demo = build(brain_delay=0, data_dir=root, step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        names = set(demo.tools.snapshot())
        self.assertIn("memory.search", names, names)
        self.assertIn("weather.get", names, names)
        self.assertIsNotNone(demo.locations)


if __name__ == "__main__":
    unittest.main(verbosity=2)
