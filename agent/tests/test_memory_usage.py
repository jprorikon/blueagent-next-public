#!/usr/bin/env python3
"""記憶效用記帳（owner 2026-10-24）。

為什麼要有：`memory.search` 撈出來的事實如果這一輪**根本沒用進回覆**，那是噪音；
下次還因為「出現在結果裡」被撈出來，就會自我強化（「角色卡」那件事就是這樣滾的）。
這裡只記帳（utility／misses），不改分數——先有資料，門檻之後實測定。

判據：
  ① 回覆用到那條事實（≥1/3 二字詞重疊）⇒ utility+1、last_used_at 更新
  ② 撈出來但沒用 ⇒ misses+1
  ③ 沒撈到記憶的回合 ⇒ 什麼都不記（不要製造假資料）
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.adapter.memory_sqlite import SqliteMemory  # noqa: E402
from agent.brain.compose.stages import MemoryUsageStage, _overlaps  # noqa: E402

FACT = "主人正在讀佐々木とピーちゃんという小説"


class TestOverlap(unittest.TestCase):
    def test_reply_that_uses_the_fact_counts(self):
        self.assertTrue(_overlaps(FACT, "佐々木とピーちゃん、面白いよね。小説の話しようか", 0.34))

    def test_unrelated_reply_does_not(self):
        self.assertFalse(_overlaps(FACT, "今日はいい天気だね", 0.34))

    def test_empty_reply_is_never_used(self):
        self.assertFalse(_overlaps(FACT, "", 0.34))


class TestUsageStage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = SqliteMemory(pathlib.Path(self.tmp.name) / "f.db", seed=False)
        self.memory.add(FACT, layer="pair", audience={"owner", "mira"}, entity="mira")
        self.memory.add("主人喜歡ほうじ茶", layer="pair",
                        audience={"owner", "mira"}, entity="mira")

    def rows(self) -> dict[str, dict]:
        return {row["text"]: row for row in self.memory.snapshot()["facts"]}

    def run_stage(self, reply: str, hits: list[str]):
        recorded: dict = {}

        def usage(*, used, missed):
            recorded["used"] = used
            recorded["missed"] = missed
            if used:
                self.memory.note_usage(used, used=True)
            if missed:
                self.memory.note_usage(missed, used=False)

        env = type("Env", (), {})()
        env.meta = {"memory_hits": hits, "reply_text": reply}
        MemoryUsageStage(usage).run(env, type("Ctx", (), {"emit": lambda *a, **k: None})())
        return recorded

    def test_used_fact_gets_utility(self):
        recorded = self.run_stage("佐々木とピーちゃん、面白いよね", [FACT, "主人喜歡ほうじ茶"])
        self.assertEqual(recorded["used"], [FACT])
        self.assertEqual(recorded["missed"], ["主人喜歡ほうじ茶"])
        rows = self.rows()
        self.assertEqual(rows[FACT]["utility"], 1)
        self.assertEqual(rows[FACT]["misses"], 0)
        self.assertEqual(rows["主人喜歡ほうじ茶"]["misses"], 1)
        self.assertEqual(rows[FACT]["usefulness"], 1.0)
        self.assertEqual(rows["主人喜歡ほうじ茶"]["usefulness"], 0.0)

    def test_no_hits_means_no_record(self):
        recorded = self.run_stage("うん", [])
        self.assertEqual(recorded, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
