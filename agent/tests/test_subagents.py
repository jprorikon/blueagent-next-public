#!/usr/bin/env python3
"""子代理（內在並行思考）：單行動主線不變。

判據：
  ① 子代理只讀：跑完之後記憶／原文條數不變（沒有任何副作用）；
  ② 筆記只進 prompt 的 `[[notes]]` 段，不進對話、不進記憶；
  ③ 有預算：慢的子代理超時被放棄，不擋主線；
  ④ 管線：chat.text／voice.realtime／trpg.turn 都在 recall 之後插了 `subagents`。
"""

from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.messages import build_messages
from agent.brain.ports import BrainRequest
from agent.brain.pipelines.blueprints import BLUEPRINTS
from agent.brain.runtime.subagents import InnerVoice, RecallScout, SubAgentOrchestrator


class FakeSearch:
    def __init__(self):
        self.calls = 0

    def search(self, *, entity, present, query, limit=5, since=None, until=None, layers=None,
               stream="", game=None):        # 與 MemorySearch.search 同一個簽名
        self.calls += 1
        return {"ok": True, "query": query,
                "facts": [{"text": "主人在读佐々木とピーちゃん。"}],
                "windows": [{"topic": "在讀的小說", "summary": "主人提到讀到第六卷，ミラ說也想讀。"}],
                "turns": [{"speaker": "主人", "text": "今、小説読んでるよ。"}],
                "hits": 3}


def request(notes) -> BrainRequest:
    return BrainRequest(entity="mira", principal="owner", thread_id="th-1",
                        text="小説の話おぼえてる？",
                        system="你是ミラ。", meta={"subagent_notes": notes})


class TestSubAgents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.memory = SqliteMemory(self.root / "facts.db", seed=False)
        self.memory.add("主人在读《佐々木とピーちゃん》。", layer="pair",
                        audience={"mira", "owner"}, entity="mira")

    def count(self) -> int:
        db = sqlite3.connect(self.root / "facts.db")
        total = db.execute("SELECT count(*) FROM facts").fetchone()[0]
        db.close()
        return total

    def test_notes_are_read_only(self):
        before = self.count()
        orchestrator = SubAgentOrchestrator([RecallScout(FakeSearch())])
        notes = orchestrator.think(entity="mira", present={"mira", "owner"},
                                   text="小説の話おぼえてる？")
        self.assertTrue(notes, notes)
        self.assertEqual(self.count(), before, "子代理不得寫記憶")

    def test_inner_voice_adds_first_person_note(self):
        calls = {"n": 0}

        def fake_call(prompt: str) -> str:
            calls["n"] += 1
            return "有點開心，又有點怕劇透；想問他讀到哪裡。"

        orchestrator = SubAgentOrchestrator([RecallScout(FakeSearch()), InnerVoice(fake_call)])
        notes = orchestrator.think(entity="mira", present={"mira", "owner"},
                                   text="小説の話おぼえてる？")
        self.assertEqual(calls["n"], 1)
        rendered = [note.render() for note in notes]
        self.assertTrue(any("[inner]" in line for line in rendered), rendered)

    def test_slow_agent_times_out_without_blocking(self):
        class Slow:
            name = "slow"
            read_only = True

            def available(self):
                return True

            def run(self, **_kwargs):
                time.sleep(1.5)
                return ["太慢了"]

        orchestrator = SubAgentOrchestrator([Slow()], timeout_s=0.2, max_agents=1)
        started = time.time()
        notes = orchestrator.think(entity="mira", present={"mira"}, text="何か話そう")
        self.assertLess(time.time() - started, 1.2, "超時應該立刻放棄")
        self.assertTrue(any("時間切れ" in note.text for note in notes), notes)

    def test_notes_reach_prompt_but_not_history(self):
        orchestrator = SubAgentOrchestrator([RecallScout(FakeSearch())])
        notes = [note.render() for note in orchestrator.think(
            entity="mira", present={"mira", "owner"}, text="小説の話おぼえてる？")]
        messages = build_messages(request(notes))
        joined = "\n".join(str(item.get("content") or "") for item in messages)
        self.assertIn("[[notes]]", joined)
        self.assertIn("在讀的小說", joined)
        # 歷史裡不該有這些筆記（它們是內心的，不是對話）
        self.assertFalse(any(str(item.get("role")) == "assistant" for item in messages))

    def test_blueprints_put_subagents_after_recall(self):
        for pipeline in ("chat.text", "voice.realtime", "trpg.turn", "chat.work"):
            stages = list(BLUEPRINTS[pipeline])
            self.assertIn("subagents", stages, pipeline)
            self.assertLess(stages.index("recall"), stages.index("subagents"), pipeline)
            self.assertLess(stages.index("subagents"), stages.index("persona"), pipeline)


if __name__ == "__main__":
    unittest.main(verbosity=2)
