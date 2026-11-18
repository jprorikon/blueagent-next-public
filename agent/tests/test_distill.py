#!/usr/bin/env python3
"""背景提煉（`runtime/distill.py`）：session 過濾 → 門檻入檔 → 履歷可查。

判據：
  ① session 切分：同 thread 內隔超過門檻就算新的一段；
  ② 規則過濾：太短／輪數不夠 ⇒ 不送 LLM（raw 照樣留著）；
  ③ 門檻：conf < 0.6 只記 `skip`、不入檔；conf ≥ 0.6 才進記憶；
  ④ 履歷：`facts_history` 真的有 insert / skip 兩種列，run 檔也有；
  ⑤ 冪等：同一批 session 跑第二次不會重複入檔；
  ⑥ LLM 失敗 ⇒ 該 session 不記水位（下輪重試），不寫半條記憶。
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
from agent.brain.runtime.distill import Distiller
from agent.brain.runtime.rawlog import RawLog


class FakeThread:
    def __init__(self, entity, thread_id, participants, turns):
        self.key = ThreadKey(scope="owner", entity=entity, scene="chat", purpose="talk")
        self.id = thread_id
        self.participants = set(participants)
        self.segment = list(turns)


def turn(turn_id, text, *, at, role="user"):
    return TurnRecord(turn_id=turn_id, role=role, text=text, source="app", at=at,
                      speaker="owner" if role == "user" else "mira")


class DistillCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.memory = SqliteMemory(self.root / "memory" / "facts.db", seed=False)
        self.log = RawLog(self.root / "raw")
        self.calls: list[str] = []

    def seed_session(self, *, texts=None, count: int = 10):
        """造一個夠大的窗口（預設 10 輪，> MIN_TURNS=8）。"""
        now = time.time() - 7200
        base = texts or ["今、小説読んでるよ。佐々木とピーちゃんっていう小説で、六巻まで読んだ。"]
        turns = []
        for index in range(count):
            text = str(base[index % len(base)]) + f"（{index + 1}）"
            turns.append(turn(f"t{index + 1}", text, at=now + index * 20,
                              role="user" if index % 2 == 0 else "assistant"))
        self.log.sync_thread(FakeThread("mira", "th-1", {"owner"}, turns))
        return now

    def distiller(self, transport) -> Distiller:
        return Distiller(memory=self.memory, raw_log=self.log, entities=["mira"],
                         transport=transport, clock=lambda: 1_700_000_000.0)

    def history(self) -> list[dict]:
        db = sqlite3.connect(self.root / "memory" / "facts.db")
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT * FROM facts_history").fetchall()]
        db.close()
        return rows


class TestDistill(DistillCase):
    def test_inserts_above_threshold_and_records_history(self):
        self.seed_session()
        payload = {"candidates": [
            {"text": "主人在读《佐々木とピーちゃん》，读到第六卷。", "layer": "pair",
             "confidence": 0.92, "evidence": ["t1", "t2"]},
            {"text": "主人好像喜欢冒险类小说。", "layer": "pair", "confidence": 0.3},
        ]}
        distiller = self.distiller(lambda prompt: json.dumps(payload, ensure_ascii=False))
        result = distiller.run_once()
        self.assertEqual(result["facts_added"], 1)
        self.assertEqual(result["facts_skipped"], 1)
        texts = [row["text"] for row in self.memory.snapshot(entity="mira")["facts"]]
        self.assertTrue(any("佐々木" in text for text in texts), texts)
        self.assertFalse(any("冒险" in text for text in texts), texts)
        rows = self.history()
        self.assertEqual({row["action"] for row in rows}, {"insert", "skip"})
        self.assertTrue(any(row["reason"] == "below_threshold" for row in rows))
        # 履歷檔
        run_files = list((self.root / "memory" / "distill").glob("*.json"))
        self.assertTrue(run_files)

    def test_prompt_sent_to_the_model_is_japanese(self):
        """★ review 2026-10-28：提煉指令以前整段繁中（「你是記憶提煉器」）⇒ 摘要／事實跟著寫成中文。"""
        from prompt_lang import chinese_hits
        self.seed_session()
        seen: list[str] = []

        def transport(prompt: str) -> str:
            seen.append(prompt)
            return json.dumps({"candidates": []})

        self.distiller(transport).run_once()
        self.assertTrue(seen)
        instruction = seen[0].split("[t1]")[0]         # 指令＋開頭；對話原文本身不算
        self.assertEqual(chinese_hits(instruction), [])
        for key in ('"window"', '"candidates"', "pair", "preference|state|event|rule", "「主人」"):
            self.assertIn(key, instruction, "JSON 欄位／列舉值／主語寫法不能因為翻譯走樣")

    def test_second_run_is_idempotent(self):
        self.seed_session()
        payload = {"candidates": [{"text": "主人在读《佐々木とピーちゃん》。", "layer": "pair",
                                   "confidence": 0.9}]}
        distiller = self.distiller(lambda prompt: json.dumps(payload, ensure_ascii=False))
        first = distiller.run_once()
        second = distiller.run_once()
        self.assertEqual(first["facts_added"], 1)
        self.assertEqual(second["facts_added"], 0)
        self.assertGreater(second["sessions_skipped"], 0)

    def test_short_session_is_skipped_without_llm(self):
        self.seed_session(texts=["うん", "そうだね"], count=3)
        called = {"n": 0}

        def transport(prompt: str) -> str:
            called["n"] += 1
            return '{"candidates":[]}'

        result = self.distiller(transport).run_once()
        self.assertEqual(called["n"], 0, "太短的一段不該送 LLM")
        self.assertEqual(result["facts_added"], 0)
        self.assertEqual(result["sessions_total"], 0, "不足最小窗口的根本不列入提煉")

    def test_llm_failure_does_not_mark_done(self):
        self.seed_session()

        def broken(prompt: str) -> str:
            raise RuntimeError("boom")

        distiller = self.distiller(broken)
        result = distiller.run_once()
        self.assertEqual(result["facts_added"], 0)
        self.assertTrue(result["errors"])
        self.assertEqual(result["sessions_done"], 0)

    def test_task_instruction_is_not_a_memory(self):
        self.seed_session()
        payload = {"candidates": [
            {"text": "主人要求把 API 改成 SSE，之後 Unity 按此開發。", "layer": "consensus",
             "confidence": 0.95},
        ]}
        result = self.distiller(lambda prompt: json.dumps(payload, ensure_ascii=False)).run_once()
        self.assertEqual(result["facts_added"], 0)
        self.assertEqual(result["facts_skipped"], 1)
        self.assertTrue(any(row["reason"] == "task_instruction" for row in self.history()))

    def test_question_is_not_a_memory(self):
        self.seed_session()
        payload = {"candidates": [{"text": "主人询问她是否移植了拆分回复？", "layer": "pair",
                                   "confidence": 0.95}]}
        result = self.distiller(lambda prompt: json.dumps(payload, ensure_ascii=False)).run_once()
        self.assertEqual(result["facts_added"], 0)
        self.assertTrue(any(row["reason"] == "question_not_fact" for row in self.history()))

    def test_near_duplicate_is_skipped(self):
        self.memory.add("主人与ミラ约定今晚一起准备角色卡。", layer="pair",
                        audience={"mira", "owner"}, entity="mira", source="distill")
        self.seed_session()
        payload = {"candidates": [{"text": "主人与ミラ约定今晚一起准备角色卡", "layer": "pair",
                                   "confidence": 0.9}]}
        result = self.distiller(lambda prompt: json.dumps(payload, ensure_ascii=False)).run_once()
        self.assertEqual(result["facts_added"], 0)
        self.assertTrue(any(row["reason"] == "near_duplicate" for row in self.history()))

    def test_owner_is_written_as_master(self):
        self.seed_session()
        payload = {"candidates": [{"text": "owner 喜欢ほうじ茶。", "layer": "pair",
                                   "confidence": 0.9}]}
        distiller = Distiller(memory=self.memory, raw_log=self.log, entities=["mira"],
                              transport=lambda prompt: json.dumps(payload, ensure_ascii=False),
                              clock=lambda: 1_700_000_000.0,
                              names={"mira": "ミラ"})
        result = distiller.run_once()
        self.assertEqual(result["facts_added"], 1)
        texts = [row["text"] for row in self.memory.snapshot(entity="mira")["facts"]]
        self.assertTrue(any(text.startswith("主人") for text in texts), texts)
        self.assertFalse(any(text.startswith("owner") for text in texts), texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
