#!/usr/bin/env python3
"""記憶分流的第 2 步：池子拆分 ＋ 檢索路由 ＋ 開關（owner 2026-11-03）。

判據：
  ① 指定 `stream` 召回時，只會拿到那一條流（`stream=''` 的舊資料視同 dialogue）；
  ② 主對話走路由時**分兩次取**（對話流 + 生活流小格），對話流在前；
  ③ `BLUEAGENT_STREAM_ROUTING=0` ⇒ 回到分流前的單次召回（同一段程式、可 A/B）；
  ④ `MemorySearch` 的候選池也分兩邊取——不然 600 條窗口還是被生活流水灌滿。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent" / "tests"))

from agent.adapter.memory_sqlite import SqliteMemory  # noqa: E402
from agent.brain.compose.stages.context import TurnContext  # noqa: E402
from agent.brain.compose.stages.recall import RecallStage  # noqa: E402
from agent.brain.domain.thread import MemoryScope  # noqa: E402
from agent.brain.memory.facts import STREAM_DIALOGUE, STREAM_LIFE  # noqa: E402
from agent.brain.runtime.memory.memory_search import MemorySearch  # noqa: E402


class _Ports:
    """假的記憶埠：記下每次呼叫的參數，回固定的兩條事實。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def recall_detail(self, scope, text, *, limit=4, participants=(), stream: str = "",
                      game: int | None = None):
        self.calls.append({"limit": limit, "stream": stream, "game": game})
        tag = stream or "mixed"
        return [SimpleNamespace(text=f"{tag}-{index}", recall_text=f"{tag}-{index}",
                                stream=stream or STREAM_DIALOGUE, to_dict=lambda: {})
                for index in range(limit)]


def _env():
    scope = MemoryScope(principal="owner", entity="mira", scene="together",
                        thread_id="th", self_owned=False)
    return SimpleNamespace(text="覚えてる？",
                           decision=SimpleNamespace(scope=scope, principal="owner"),
                           event=SimpleNamespace(participants=("owner",)),
                           meta={})


class _Ctx:
    def __init__(self, ports) -> None:
        self.ports = ports
        self.events: list = []

    def emit(self, name, payload=None):
        self.events.append((name, payload))

    def mark(self, name):
        self.events.append((name, None))


class TestRecallRouting(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("BLUEAGENT_STREAM_ROUTING")
        os.environ["BLUEAGENT_STREAM_ROUTING"] = "1"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("BLUEAGENT_STREAM_ROUTING", None)
        else:
            os.environ["BLUEAGENT_STREAM_ROUTING"] = self._old

    def test_routing_calls_both_streams_with_dialogue_first(self):
        ports = _Ports()
        env = _env()
        RecallStage().run(env, _Ctx(ports))
        streams = [call["stream"] for call in ports.calls]
        self.assertEqual(streams[0], STREAM_LIFE)      # 先算生活流那一格（決定對話流留幾位）
        self.assertIn(STREAM_DIALOGUE, streams)
        self.assertEqual(len(ports.calls), 2)
        facts = TurnContext.of(env).recall.recall
        self.assertTrue(facts[0].startswith("dialogue"))   # 對話流排在前面
        self.assertTrue(any(str(item).startswith("life") for item in facts))

    def test_switch_off_goes_back_to_a_single_call(self):
        os.environ["BLUEAGENT_STREAM_ROUTING"] = "0"
        ports = _Ports()
        env = _env()
        RecallStage().run(env, _Ctx(ports))
        self.assertEqual(len(ports.calls), 1)
        self.assertEqual(ports.calls[0]["stream"], "")

    def test_life_slot_respects_env(self):
        os.environ["BLUEAGENT_RECALL_LIFE"] = "1"
        try:
            ports = _Ports()
            RecallStage().run(_env(), _Ctx(ports))
            life_call = next(call for call in ports.calls if call["stream"] == STREAM_LIFE)
            self.assertEqual(life_call["limit"], 1)
        finally:
            os.environ.pop("BLUEAGENT_RECALL_LIFE", None)


class TestStreamFilteredRecall(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-routing-"))
        self.db = self.root / "facts.db"
        self.memory = SqliteMemory(self.db, entity_ids=("mira",))
        self.memory.add("主人は来週試験があると言った。", source="distill", entity="mira",
                        layer="pair", audience={"mira", "owner"}, principal="owner")
        self.memory.add("今日はノートを読み返した。", source="self_note", entity="mira",
                        layer="self")
        scope = MemoryScope(principal="owner", entity="mira", scene="together",
                            thread_id="th", self_owned=False)
        self.scope = scope

    def tearDown(self):
        self.memory.close()

    def test_dialogue_and_life_are_separated(self):
        dialogue = self.memory.recall_detail(self.scope, "試験 ノート", limit=8,
                                             participants={"owner"}, stream=STREAM_DIALOGUE)
        life = self.memory.recall_detail(self.scope, "試験 ノート", limit=8,
                                         participants={"owner"}, stream=STREAM_LIFE)
        self.assertEqual([fact.stream for fact in dialogue], [STREAM_DIALOGUE])
        self.assertEqual([fact.stream for fact in life], [STREAM_LIFE])

    def test_no_stream_keeps_both(self):
        both = self.memory.recall_detail(self.scope, "試験 ノート", limit=8,
                                         participants={"owner"})
        self.assertEqual(len(both), 2)


class _FakeSearch:
    """假的混合檢索：回一條事實＋一條窗口摘要，並記下被要求的流。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(self, *, entity, present, query, limit=5, since=None, until=None,
               layers=("fact", "window", "turn"), stream: str = "",
               game: int | None = None) -> dict:
        self.calls.append({"limit": limit, "stream": stream, "layers": tuple(layers), "game": game})
        return {"ok": True,
                "facts": [{"text": "三日前に交わした約束の話", "layer": "pair"}],
                "windows": [{"topic": "約束", "summary": "来週の試験の話をした", "turns": 12}]}


class TestHybridRecall(unittest.TestCase):
    def setUp(self):
        os.environ["BLUEAGENT_STREAM_ROUTING"] = "1"

    def tearDown(self):
        os.environ.pop("BLUEAGENT_STREAM_ROUTING", None)

    def test_dialogue_slot_uses_hybrid_search_with_windows(self):
        search = _FakeSearch()
        ports = _Ports()
        env = _env()
        RecallStage(search).run(env, _Ctx(ports))
        self.assertEqual(len(search.calls), 1)
        self.assertEqual(search.calls[0]["stream"], STREAM_DIALOGUE)
        self.assertIn("window", search.calls[0]["layers"])
        facts = [str(item) for item in TurnContext.of(env).recall.recall]
        self.assertIn("三日前に交わした約束の話", facts)          # 事實層進來了
        self.assertTrue(any("以前のやりとり" in item for item in facts))   # 窗口摘要也進來了
        # 生活流那一格仍然走記憶埠（字面召回）
        self.assertIn(STREAM_LIFE, [call["stream"] for call in ports.calls])

    def test_without_search_falls_back_to_literal(self):
        ports = _Ports()
        RecallStage().run(_env(), _Ctx(ports))
        self.assertEqual([call["stream"] for call in ports.calls].count(STREAM_DIALOGUE), 1)


class TestPoolSplit(unittest.TestCase):
    def test_pool_is_taken_per_stream(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-pool-"))
        db = root / "facts.db"
        memory = SqliteMemory(db, entity_ids=("mira",))
        for index in range(5):
            memory.add(f"生活の記録{index}", source="self_note", entity="mira", layer="self")
        memory.add("主人との約束", source="distill", entity="mira", layer="pair",
                   audience={"mira", "owner"}, principal="owner")
        search = MemorySearch(memory=memory, db_path=db)
        life = search._fact_pool("mira", {"mira", "owner"}, stream=STREAM_LIFE)
        dialogue = search._fact_pool("mira", {"mira", "owner"}, stream=STREAM_DIALOGUE)
        memory.close()
        self.assertEqual(len(life), 5)
        self.assertEqual([fact.text for fact in dialogue], ["主人との約束"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
