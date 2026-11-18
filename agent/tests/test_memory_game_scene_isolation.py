#!/usr/bin/env python3
"""TRPG／現實記憶隔離的**旁路**（2026-11-15 審核；審核檔已結項清出，見 `git log`）。

`test_memory_game_scene.py` 守的是主路（蒸餾打標＋被動召回過濾）。這一支守審核抓到的旁路：
  ① 回合內的規則抽取（「記住／我喜歡／叫我」）在桌上也要打 `game=1`，鏡像出去的那份也是；
  ② `memory.search`／`memory.open`／子代理檢索照回合的場景走，事實／原文兩層都不串；
  ③ 取代不跨場景：桌上的新值收不掉現實的現值（反之亦然），同場景照常取代；
  ④ 從 jsonl 重建索引之後 `game` 還在（索引可以丟，場景不能丟）。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.domain.thread import MemoryScope, ThreadKey, TurnRecord
from agent.brain.memory.fact_store import FactStore
from agent.brain.memory.facts import game_of
from agent.brain.memory.in_memory import InMemoryMemory
from agent.brain.runtime.memory.memory_search import MemorySearch
from agent.brain.runtime.rawlog import RawLog
from agent.brain.runtime.subagents import RecallScout
from agent.brain.runtime.tools.base import ToolContext
from agent.brain.runtime.tools.memory import MemoryOpenTool, MemorySearchTool

REAL = "主人は現実で小説を読んでいる"
GAME = "主人はゲームで港町にいる"
SLOT = "主人の現在地"
PAIR = {"mira", "owner"}


def turn(source: str, text: str = "记住：我喜欢港町") -> TurnRecord:
    return TurnRecord(turn_id="g1", role="user", text=text, source=source, speaker="owner", at=1.0)


def scope() -> MemoryScope:
    return MemoryScope(principal="owner", entity="mira", scene="chat", thread_id="th")


def context(source: str) -> ToolContext:
    return ToolContext(entity="mira", principal="owner", thread_id="t", source=source,
                       voice=None, present=frozenset({"owner"}))


class Line:
    """給 RawLog 用的假 thread。"""

    def __init__(self, thread_id: str, turns, *, purpose: str) -> None:
        self.key = ThreadKey(scope="owner", entity="mira", scene="chat", purpose=purpose)
        self.id = thread_id
        self.participants = {"owner"}
        self.segment = list(turns)


class Backends(unittest.TestCase):
    """兩個後端跑同一組斷言（規則只有一份，落存各一份 ⇒ 兩邊都要守）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def backends(self, **kwargs):
        self.round = getattr(self, "round", 0) + 1          # 每次要一個乾淨的庫
        yield "sqlite", SqliteMemory(self.root / f"sqlite-{self.round}" / "facts.db", seed=False,
                                     **kwargs)
        yield "memory", InMemoryMemory(**kwargs)

    @staticmethod
    def facts(memory, entity: str = "mira") -> list[dict]:
        return memory.snapshot(None, entity=entity)["facts"]

    def fact(self, memory, text: str) -> dict:
        return next(row for row in self.facts(memory) if row["text"] == text)


class TestSceneOfASource(unittest.TestCase):
    def test_one_predicate(self):
        self.assertEqual(game_of("trpg:table-1"), 1)
        self.assertEqual(game_of("phone:owner"), 0)
        self.assertEqual(game_of("phone:owner", "trpg"), 1, "raw 行有 purpose=trpg 就算桌上")
        self.assertEqual(game_of(None, None), 0)


class TestImmediateFactsCarryTheScene(Backends):
    def test_table_turn_writes_game_facts(self):
        for name, memory in self.backends():
            with self.subTest(name):
                memory.record(scope(), turn("trpg:table-1"), participants=PAIR)
                games = [row["game"] for row in self.facts(memory)]
                self.assertTrue(games, "這句應該命中「記住」「我喜歡」兩條規則")
                self.assertEqual(set(games), {1})

    def test_reality_turn_stays_reality(self):
        for name, memory in self.backends():
            with self.subTest(name):
                memory.record(scope(), turn("phone:owner"), participants=PAIR)
                self.assertEqual({row["game"] for row in self.facts(memory)}, {0})

    def test_mirror_inherits_the_scene(self):
        for name, memory in self.backends(entity_ids=("mira", "aster")):
            with self.subTest(name):
                memory.record(scope(), turn("trpg:table-1"), participants=PAIR | {"aster"})
                mirrored = self.facts(memory, "aster")
                self.assertTrue(mirrored, "另一個角色的倉應該有鏡像")
                self.assertEqual({row["game"] for row in mirrored}, {1})

    def test_table_fact_is_not_recalled_in_reality(self):
        for name, memory in self.backends():
            with self.subTest(name):
                memory.record(scope(), turn("trpg:table-1"), participants=PAIR)
                self.assertEqual(memory.recall_detail(scope(), "港町", participants=PAIR, game=0), [])
                self.assertTrue(memory.recall_detail(scope(), "港町", participants=PAIR, game=1))


class TestSupersedeStaysInsideTheScene(Backends):
    def write(self, memory, text: str, game: int, **kwargs):
        return memory.add(text, layer="pair", entity="mira", audience=PAIR, source="distill",
                          kind="state", slot=SLOT, game=game, **kwargs)

    def test_game_state_does_not_retire_reality(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.write(memory, REAL, 0)
                self.assertEqual(memory.supersede_slot(entity="mira", slot=SLOT, new_key=GAME,
                                                       at=1000, game=1), 0)
                self.write(memory, GAME, 1)
                old = self.fact(memory, REAL)
                self.assertEqual((old["superseded_by"], old["active"]), ("", True))

    def test_reality_state_does_not_retire_the_game(self):
        for name, memory in self.backends():
            with self.subTest(name):
                self.write(memory, GAME, 1)
                self.assertEqual(memory.supersede_slot(entity="mira", slot=SLOT, new_key=REAL,
                                                       at=1000, game=0), 0)
                self.write(memory, REAL, 0)
                self.assertEqual(self.fact(memory, GAME)["superseded_by"], "")

    def test_same_scene_still_supersedes(self):
        for game in (0, 1):
            for name, memory in self.backends():
                with self.subTest(f"{name}/game={game}"):
                    self.write(memory, REAL, game)
                    self.assertEqual(memory.supersede_slot(entity="mira", slot=SLOT,
                                                           new_key=GAME, at=1000, game=game), 1)
                    new = self.write(memory, GAME, game)
                    old = self.fact(memory, REAL)
                    self.assertEqual(old["superseded_by"], new.key)
                    self.assertFalse(old["active"])

    def test_caller_that_forgot_the_scene_is_repaired_on_add(self):
        """先 `supersede_slot()`（沒說場景＝當成現實）再落一條桌上的 ⇒ 現實那條要被放回現值。"""
        for name, memory in self.backends():
            with self.subTest(name):
                self.write(memory, REAL, 0)
                memory.supersede_slot(entity="mira", slot=SLOT, new_key=GAME, at=1000)
                new = self.write(memory, GAME, 1)
                old = self.fact(memory, REAL)
                self.assertEqual((old["superseded_by"], old["active"]), ("", True))
                self.assertEqual(tuple(new.supersedes), ())

    def test_explicit_supersedes_do_not_cross(self):
        for name, memory in self.backends():
            with self.subTest(name):
                old = self.write(memory, REAL, 0)
                new = memory.add(GAME, layer="pair", entity="mira", audience=PAIR,
                                 source="distill", game=1, supersedes=[old.key])
                self.assertEqual(self.fact(memory, REAL)["superseded_by"], "")
                self.assertNotEqual(self.fact(memory, GAME)["key"], "")
                self.assertTrue(new.key)


class TestRebuildKeepsTheScene(unittest.TestCase):
    def test_write_rebuild_recall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            memory = SqliteMemory(root / "facts.db", store=FactStore(root / "facts.jsonl"), seed=False)
            memory.add(GAME, layer="pair", entity="mira", audience=PAIR, source="distill",
                       game=1, topic="卓")
            memory.add(REAL, layer="pair", entity="mira", audience=PAIR, source="distill")
            self.assertEqual(memory.rebuild(), 2)
            rebuilt = memory.fact_by_text(GAME, entity="mira")
            self.assertEqual((rebuilt.game, rebuilt.topic), (1, "卓"))
            self.assertEqual(memory.fact_by_text(REAL, entity="mira").game, 0)
            reality = [fact.text for fact in memory.recall_detail(scope(), "主人", participants=PAIR,
                                                                  game=0)]
            self.assertEqual(reality, [REAL], "重建之後桌上的事實不能回到現實召回裡")


class TestActiveSearchFollowsTheTurn(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.memory = SqliteMemory(root / "facts.db", seed=False)
        self.memory.add(REAL, layer="pair", entity="mira", audience=PAIR, source="distill")
        self.memory.add(GAME, layer="pair", entity="mira", audience=PAIR, source="distill", game=1)
        log = RawLog(root / "raw")
        log.sync_thread(Line("th-real", [TurnRecord(
            turn_id="r1", role="user", text=REAL, source="phone:owner", speaker="owner", at=10.0)],
            purpose="talk"))
        log.sync_thread(Line("th-game", [TurnRecord(
            turn_id="g1", role="user", text=GAME, source="trpg:table-1", speaker="owner", at=20.0)],
            purpose="trpg"))
        self.search = MemorySearch(memory=self.memory, raw_log=log, db_path=self.memory.path)

    def run_tool(self, source: str, layers: str) -> dict:
        result = MemorySearchTool(self.search).run(context(source), {"query": "主人", "layers": layers})
        self.assertTrue(result.ok, result.error)
        return result.data

    def test_fact_layer(self):
        self.assertEqual([row["text"] for row in self.run_tool("phone:owner", "fact")["facts"]], [REAL])
        self.assertEqual([row["text"] for row in self.run_tool("trpg:table-1", "fact")["facts"]], [GAME])

    def test_turn_layer(self):
        self.assertEqual([row["text"] for row in self.run_tool("phone:owner", "turn")["turns"]], [REAL])
        self.assertEqual([row["text"] for row in self.run_tool("trpg:table-1", "turn")["turns"]], [GAME])

    def test_direct_search_filters_raw_turns(self):
        found = self.search.search(entity="mira", present={"owner"}, query="港町",
                                   layers=("turn",), game=0)
        self.assertEqual(found["turns"], [])
        unscoped = self.search.search(entity="mira", present={"owner"}, query="港町",
                                      layers=("turn",))
        self.assertEqual([row["text"] for row in unscoped["turns"]], [GAME], "game=None＝不過濾")

    def test_known_pointer_cannot_cross(self):
        """拿著桌上那一輪的門牌，在現實回合開不出來（否則 search 的過濾等於沒做）。"""
        pointer = self.run_tool("trpg:table-1", "turn")["turns"][0]["ptr"]
        opened = MemoryOpenTool(self.search).run(context("trpg:table-1"), {"ptr": pointer})
        self.assertTrue(opened.ok, opened.error)
        self.assertEqual([row["text"] for row in opened.data["rows"]], [GAME])
        blocked = MemoryOpenTool(self.search).run(context("phone:owner"), {"ptr": pointer})
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error, "turn_not_found")

    def test_recall_scout_passes_the_scene(self):
        scout = RecallScout(self.search, limit=3)
        reality = scout.run(entity="mira", present={"owner"}, text="主人", meta={"game": 0})
        table = scout.run(entity="mira", present={"owner"}, text="主人", meta={"game": 1})
        self.assertTrue(reality and table)
        self.assertFalse([line for line in reality if "ゲーム" in line])
        self.assertFalse([line for line in table if "現実" in line])


class TestCrossSceneDedupeFirstWins(Backends):
    """這是 owner 2026-11-15 拍板的現狀，不是 bug；要改需改表（生產庫 309MB）。

    去重唯一鍵維持 `(norm, entity)`，不加 `game`。同一句話跨場景寫入時只留先到的那條。
    """

    SAME = "私は港町の海風が好きなんだ"

    def test_table_first_then_reality_keeps_only_game_one(self):
        """這是 owner 2026-11-15 拍板的現狀，不是 bug；要改需改表（生產庫 309MB）。"""
        for name, memory in self.backends():
            with self.subTest(name):
                memory.add(self.SAME, layer="pair", entity="mira", audience=PAIR,
                           source="distill", game=1)
                memory.add(self.SAME, layer="pair", entity="mira", audience=PAIR,
                           source="distill", game=0)
                rows = [row for row in self.facts(memory) if row["text"] == self.SAME]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["game"], 1)
                self.assertEqual(memory.recall_detail(scope(), "海風", participants=PAIR, game=0), [])
                self.assertEqual(len(memory.recall_detail(scope(), "海風", participants=PAIR, game=1)), 1)

    def test_reality_first_then_table_keeps_only_game_zero(self):
        """這是 owner 2026-11-15 拍板的現狀，不是 bug；要改需改表（生產庫 309MB）。"""
        for name, memory in self.backends():
            with self.subTest(name):
                memory.add(self.SAME, layer="pair", entity="mira", audience=PAIR,
                           source="distill", game=0)
                memory.add(self.SAME, layer="pair", entity="mira", audience=PAIR,
                           source="distill", game=1)
                rows = [row for row in self.facts(memory) if row["text"] == self.SAME]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["game"], 0)
                self.assertEqual(len(memory.recall_detail(scope(), "海風", participants=PAIR, game=0)), 1)
                self.assertEqual(memory.recall_detail(scope(), "海風", participants=PAIR, game=1), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
