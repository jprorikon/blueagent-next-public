#!/usr/bin/env python3
"""TRPG／現實在記憶層的區分（owner 2026-11-11 的 B3＋B4）。

判據：
  ① 寫入側（B3）：桌那幾窗被標成 `game=1`，提煉 prompt 看得到「這是遊戲」那一行，
     抽出來的事實與窗口摘要都帶 `game=1`；現實窗口照舊 `game=0`；
  ② 讀取側（B4）：現實回合的召回拿不到桌上的事實，桌上回合拿不到現實的事實；
  ③ 窗口摘要那一側同樣分（心跳的「最近話したこと」不能拿到桌那段）；
  ④ 舊庫／舊資料降級：沒有 `game` 欄的庫會被就地補上（預設 0＝現實），舊摘要不炸。
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
from agent.brain.domain.thread import MemoryScope, ThreadKey, TurnRecord
from agent.brain.memory.in_memory import InMemoryMemory
from agent.brain.runtime.distill import Distiller
from agent.brain.runtime.memory.window_store import WindowStore
from agent.brain.runtime.rawlog import RawLog


def reality_turn(turn_id: str, text: str, *, at: float) -> TurnRecord:
    return TurnRecord(turn_id=turn_id, role="user", text=text, source="phone:owner", at=at,
                      speaker="owner")


def game_turn(turn_id: str, text: str, *, at: float) -> TurnRecord:
    """桌上的一輪：資料形狀上就是 `role=user / speaker=owner`（這正是要區分的原因）。"""
    return TurnRecord(turn_id=turn_id, role="user", text=text, source="trpg:table-1", at=at,
                      speaker="owner", addressed_to="entity")


class Table:
    """給 RawLog 用的假 thread（跟 test_distill 同一形狀）。"""

    def __init__(self, entity, thread_id, turns, *, purpose="talk"):
        self.key = ThreadKey(scope="owner", entity=entity, scene="chat", purpose=purpose)
        self.id = thread_id
        self.participants = {"owner"}
        self.segment = list(turns)


def window_record(*, window_id, ended_at, game=0, topic="話題", summary="摘要") -> dict:
    return {"window_id": window_id, "entity": "mira", "thread": "th-1",
            "started_at": ended_at - 600, "ended_at": ended_at, "turns": 10,
            "topic": topic, "summary": summary, "people": ["mira", "owner"],
            "open_loops": [], "run_id": "r1", "at": ended_at, "turn_ids": ["t1"], "ptr": "",
            "game": game}


class TestDistillMarksTheTable(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.memory = SqliteMemory(self.root / "memory" / "facts.db", seed=False)
        self.log = RawLog(self.root / "raw")
        self.prompts: list[str] = []

    def sync(self, *, game: bool, count: int = 10) -> float:
        now = time.time() - 3600
        make = game_turn if game else reality_turn
        turns = []
        for index in range(count):
            text = ("卓で石段に座って傷を休めてる。港町の灯りが見える。" if game
                    else "今日は小説を読んでいた。六巻まで読んだところ。")
            turns.append(make(f"t{index + 1}", f"{text}（{index + 1}）", at=now + index * 20))
        self.log.sync_thread(Table("mira", "th-1", turns, purpose="trpg" if game else "talk"))
        return now

    def distiller(self) -> Distiller:
        def transport(prompt: str) -> str:
            self.prompts.append(prompt)
            return json.dumps({"window": {"topic": "卓の様子" if "TRPG 卓" in prompt else "読書",
                                          "summary": "要約", "people": ["主人"],
                                          "open_loops": []},
                               "candidates": [{"text": "主人は港町の灯りを見ながら休んだ",
                                               "layer": "pair", "confidence": 0.9,
                                               "evidence": ["t1"], "kind": "event"}]},
                              ensure_ascii=False)
        return Distiller(memory=self.memory, raw_log=self.log, entities=["mira"],
                         transport=transport, clock=lambda: 1_700_000_000.0)

    def rows(self, table: str) -> list[dict]:
        db = sqlite3.connect(self.root / "memory" / "facts.db")
        db.row_factory = sqlite3.Row
        out = [dict(row) for row in db.execute(f"SELECT * FROM {table}").fetchall()]
        db.close()
        return out

    def test_game_window_is_rendered_as_a_game(self):
        self.sync(game=True)
        self.distiller().run_once()
        self.assertTrue(self.prompts, "應該有送出一窗")
        self.assertIn("このウィンドウは TRPG 卓", self.prompts[0])

    def test_reality_window_has_no_game_header(self):
        self.sync(game=False)
        self.distiller().run_once()
        self.assertNotIn("このウィンドウは TRPG 卓", self.prompts[0])

    def test_facts_and_window_carry_the_game_flag(self):
        self.sync(game=True)
        self.distiller().run_once()
        facts = self.rows("facts")
        self.assertEqual([row["game"] for row in facts], [1], "桌上的事必須打上 game=1")
        windows = self.rows("window_summaries")
        self.assertEqual([row["game"] for row in windows], [1])

    def test_reality_windows_stay_zero(self):
        self.sync(game=False)
        self.distiller().run_once()
        self.assertEqual([row["game"] for row in self.rows("facts")], [0])
        self.assertEqual([row["game"] for row in self.rows("window_summaries")], [0])


class TestRecallFiltersByScene(unittest.TestCase):
    """B4：現實回合只看現實、桌上回合只看桌上。"""

    def memory(self) -> SqliteMemory:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mem = SqliteMemory(pathlib.Path(tmp.name) / "facts.db", seed=False)
        mem.add("主人は港町の灯りを見ながら石段で休んだ", layer="pair", entity="mira",
                audience={"mira", "owner"}, source="distill", game=1)
        mem.add("主人は小説を六巻まで読んでいる", layer="pair", entity="mira",
                audience={"mira", "owner"}, source="distill")
        return mem

    def test_reality_turn_does_not_read_the_table(self):
        mem = self.memory()
        scope = MemoryScope(principal="owner", entity="mira", scene="chat", thread_id="th-1")
        texts = [fact.text for fact in mem.recall_detail(
            scope, "港町の灯り 小説", limit=8, participants={"owner", "mira"}, game=0)]
        self.assertTrue(any("小説" in text for text in texts))
        self.assertFalse(any("港町" in text for text in texts), texts)

    def test_table_turn_does_not_read_reality(self):
        mem = self.memory()
        scope = MemoryScope(principal="owner", entity="mira", scene="chat", thread_id="th-1")
        texts = [fact.text for fact in mem.recall_detail(
            scope, "港町の灯り 小説", limit=8, participants={"owner", "mira"}, game=1)]
        self.assertTrue(any("港町" in text for text in texts))
        self.assertFalse(any("小説" in text for text in texts), texts)

    def test_memory_backend_mirrors_the_same_rule(self):
        mem = InMemoryMemory()
        mem.add("主人は港町の灯りを見ながら休んだ", layer="pair", entity="mira",
                audience={"mira", "owner"}, game=1)
        mem.add("主人は小説を六巻まで読んでいる", layer="pair", entity="mira",
                audience={"mira", "owner"})
        scope = MemoryScope(principal="owner", entity="mira", scene="chat", thread_id="th-1")
        reality = [fact.text for fact in mem.recall_detail(
            scope, "港町 小説", limit=8, participants={"owner", "mira"}, game=0)]
        self.assertEqual(len(reality), 1)
        self.assertIn("小説", reality[0])


class TestWindowSideIsSplitToo(unittest.TestCase):
    def store(self) -> WindowStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = WindowStore(pathlib.Path(tmp.name) / "facts.db")
        store.upsert(window_record(window_id="w-reality", ended_at=1000.0, game=0,
                                   topic="読書", summary="小説の話"))
        store.upsert(window_record(window_id="w-game", ended_at=2000.0, game=1,
                                   topic="卓", summary="石段で休んだ"))
        return store

    def test_recent_can_skip_the_table(self):
        store = self.store()
        ids = [str(row["window_id"]) for row in store.recent("mira", game=0)]
        self.assertEqual(ids, ["w-reality"])

    def test_table_only(self):
        store = self.store()
        ids = [str(row["window_id"]) for row in store.recent("mira", game=1)]
        self.assertEqual(ids, ["w-game"])

    def test_old_database_gets_the_column(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name) / "facts.db"
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE window_summaries (
                window_id TEXT PRIMARY KEY, entity TEXT NOT NULL, thread TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL DEFAULT 0, ended_at REAL NOT NULL DEFAULT 0,
                turns INTEGER NOT NULL DEFAULT 0, topic TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '', people TEXT NOT NULL DEFAULT '',
                open_loops TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '',
                at REAL NOT NULL DEFAULT 0);
            INSERT INTO window_summaries (window_id, entity, ended_at, topic, summary)
            VALUES ('old', 'mira', 500, '旧', '古い摘要');
        """)
        db.commit()
        db.close()
        store = WindowStore(path)
        rows = store.recent("mira", game=0)
        self.assertEqual([str(row["window_id"]) for row in rows], ["old"],
                         "舊窗口要當成現實那側（預設 game=0），不能整條看不到")


if __name__ == "__main__":
    unittest.main(verbosity=2)
