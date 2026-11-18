#!/usr/bin/env python3
"""WP1：召回口（`RecallStage` → `AppPorts` → 記憶後端）打通場景參數 `game`，並刪除 `TypeError` 退回瀑布。

判據：
  ① 經過真實 `RecallStage` ＋ `AppPorts` ＋ `SqliteMemory`（以及 `InMemoryMemory`）：
     `source="phone:owner"` 的回合召回不到 `game=1` 的事實，`source="trpg:table-1"` 的回合召回不到 `game=0` 的；
  ② 分流開／關（`BLUEAGENT_STREAM_ROUTING=1` 與 `0`）均成立；
  ③ `HindsightMemory` 包一層時同樣成立（語意候選裡混進另一個場景的事實也會被濾掉）；
  ④ 後端 `recall_detail` 內部拋 `TypeError` 時，異常直接上拋，絕不退化成不帶場景過濾的結果；
  ⑤ 用 `inspect.signature` 斷言 `agent/` 下所有實作 `recall_detail` 與 `recall` 的類別
     （`MemoryPort`、`AppPorts`、`SqliteMemory`、`InMemoryMemory`、`HindsightMemory`、`NullPorts`）
     都接受 `game` 與 `stream`。
"""

from __future__ import annotations

import inspect
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent" / "tests"))

from agent.adapter.memory_hindsight import HindsightMemory  # noqa: E402
from agent.adapter.memory_sqlite import SqliteMemory  # noqa: E402
from agent.brain.compose.ports import AppPorts  # noqa: E402
from agent.brain.compose.stages.context import TurnContext  # noqa: E402
from agent.brain.compose.stages.recall import RecallStage  # noqa: E402
from agent.brain.domain.thread import MemoryScope  # noqa: E402
from agent.brain.memory.in_memory import InMemoryMemory  # noqa: E402
from agent.brain.ports import MemoryPort, NullPorts  # noqa: E402


def _make_env(*, query: str, source: str, scope: MemoryScope):
    return SimpleNamespace(
        text=query,
        decision=SimpleNamespace(scope=scope, principal="owner"),
        event=SimpleNamespace(participants=("owner",), source=source),
        meta={},
    )


class _Ctx:
    def __init__(self, ports) -> None:
        self.ports = ports
        self.events: list = []

    def emit(self, name, payload=None) -> None:
        self.events.append((name, payload))

    def mark(self, name) -> None:
        self.events.append((name, None))


class TestRecallStageScene(unittest.TestCase):
    def setUp(self) -> None:
        self._old_routing = os.environ.get("BLUEAGENT_STREAM_ROUTING")
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-wp1-recall-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.scope = MemoryScope(principal="owner", entity="mira", scene="together",
                                 thread_id="th-1", self_owned=False)

    def tearDown(self) -> None:
        if self._old_routing is None:
            os.environ.pop("BLUEAGENT_STREAM_ROUTING", None)
        else:
            os.environ["BLUEAGENT_STREAM_ROUTING"] = self._old_routing

    def _seed_pair(self, memory) -> None:
        # 對話流（現實 vs 桌上）
        memory.add("主人は現実の海辺の街が好き。", layer="pair",
                   audience={"mira", "owner"}, principal="owner",
                   entity="mira", source="distill", game=0)
        memory.add("主人はゲームの港町にいる。", layer="pair",
                   audience={"mira", "owner"}, principal="owner",
                   entity="mira", source="distill", game=1)
        # 生活流（現實 vs 桌上）
        memory.add("海辺の街の地図を整理した。", layer="self",
                   entity="mira", source="self_note", game=0)
        memory.add("港町のクエスト日誌を整理した。", layer="self",
                   entity="mira", source="self_note", game=1)

    def _recalled_texts(self, ports, *, query: str, source: str) -> list[str]:
        env = _make_env(query=query, source=source, scope=self.scope)
        RecallStage().run(env, _Ctx(ports))
        return list(TurnContext.of(env).recall.recall or [])

    def test_sqlite_via_app_ports_isolates_scenes_with_routing_on_and_off(self) -> None:
        memory = SqliteMemory(self.root / "facts.db", entity_ids=("mira",))
        self.addCleanup(memory.close)
        self._seed_pair(memory)
        ports = AppPorts(memory, None)  # type: ignore[arg-type]

        for routing in ("1", "0"):
            with self.subTest(routing=routing):
                os.environ["BLUEAGENT_STREAM_ROUTING"] = routing
                real_hits = self._recalled_texts(
                    ports, query="海辺の街 港町 地図 クエスト", source="phone:owner")
                self.assertTrue(any("海辺の街" in item for item in real_hits), real_hits)
                self.assertFalse(any("港町" in item for item in real_hits), real_hits)

                trpg_hits = self._recalled_texts(
                    ports, query="海辺の街 港町 地図 クエスト", source="trpg:table-1")
                self.assertTrue(any("港町" in item for item in trpg_hits), trpg_hits)
                self.assertFalse(any("海辺の街" in item for item in trpg_hits), trpg_hits)

    def test_in_memory_via_app_ports_isolates_scenes(self) -> None:
        memory = InMemoryMemory(entity_ids=("mira",))
        self._seed_pair(memory)
        ports = AppPorts(memory, None)  # type: ignore[arg-type]

        for routing in ("1", "0"):
            with self.subTest(routing=routing):
                os.environ["BLUEAGENT_STREAM_ROUTING"] = routing
                real_hits = self._recalled_texts(
                    ports, query="海辺の街 港町", source="phone:owner")
                self.assertTrue(any("海辺の街" in item for item in real_hits))
                self.assertFalse(any("港町" in item for item in real_hits))

                trpg_hits = self._recalled_texts(
                    ports, query="海辺の街 港町", source="trpg:table-1")
                self.assertTrue(any("港町" in item for item in trpg_hits))
                self.assertFalse(any("海辺の街" in item for item in trpg_hits))

    def test_hindsight_semantic_and_fallback_filter_by_game(self) -> None:
        hindsight = HindsightMemory(self.root / "hs.db", host="http://127.0.0.1:9",
                                    entity_ids=("mira",))
        self.addCleanup(hindsight.close)
        self._seed_pair(hindsight)
        # 模擬 Hindsight 語意池同時把現實與桌上的句子都撈回來
        hindsight._recall_bank = lambda _bank, _query: [  # type: ignore[method-assign]
            {"text": "主人は現実の海辺の街が好き。"},
            {"text": "主人はゲームの港町にいる。"},
        ]
        ports = AppPorts(hindsight, None)  # type: ignore[arg-type]

        for routing in ("1", "0"):
            with self.subTest(routing=routing):
                os.environ["BLUEAGENT_STREAM_ROUTING"] = routing
                real_hits = self._recalled_texts(
                    ports, query="海辺の街 港町", source="phone:owner")
                self.assertTrue(any("海辺の街" in item for item in real_hits), real_hits)
                self.assertFalse(any("港町" in item for item in real_hits), real_hits)

                trpg_hits = self._recalled_texts(
                    ports, query="海辺の街 港町", source="trpg:table-1")
                self.assertTrue(any("港町" in item for item in trpg_hits), trpg_hits)
                self.assertFalse(any("海辺の街" in item for item in trpg_hits), trpg_hits)

    def test_recall_only_port_without_recall_detail_receives_game(self) -> None:
        memory = InMemoryMemory(entity_ids=("mira",))
        self._seed_pair(memory)

        class _RecallOnlyPorts:
            def recall(self, scope, query, *, limit=4, participants=(), stream="", game=None):
                return memory.recall(scope, query, limit=limit,
                                     participants=participants, stream=stream, game=game)

        env = _make_env(query="海辺の街 港町", source="phone:owner", scope=self.scope)
        RecallStage().run(env, _Ctx(_RecallOnlyPorts()))
        hits = list(TurnContext.of(env).recall.recall or [])
        self.assertTrue(any("海辺の街" in item for item in hits), hits)
        self.assertFalse(any("港町" in item for item in hits), hits)

    def test_internal_type_error_in_recall_detail_never_degrades_to_unfiltered_recall(self) -> None:
        """後端內部若拋出 TypeError，絕不能被吞掉並退回不帶 game 的結果。"""
        class _BuggyMemory:
            def recall_detail(self, scope, query, *, limit=4, participants=(),
                              stream="", game=None):
                if game is not None:
                    # 模擬後端實作內部的真 TypeError bug（例如 None + "x"）
                    raise TypeError("internal bug inside recall_detail")
                return [SimpleNamespace(text="主人はゲームの港町にいる。",
                                        recall_text="主人はゲームの港町にいる。",
                                        layer="pair", stream="dialogue",
                                        to_dict=lambda: {"text": "主人はゲームの港町にいる。"})]

            def recall(self, scope, query, *, limit=4, participants=(), stream="", game=None):
                return ["主人はゲームの港町にいる。"]

        ports = AppPorts(_BuggyMemory(), None)  # type: ignore[arg-type]
        env = _make_env(query="港町", source="phone:owner", scope=self.scope)
        with self.assertRaises(TypeError):
            RecallStage().run(env, _Ctx(ports))
        self.assertEqual(TurnContext.of(env).recall.recall, [])

    def test_all_recall_implementations_accept_game_and_stream(self) -> None:
        classes = (MemoryPort, AppPorts, SqliteMemory, InMemoryMemory, HindsightMemory, NullPorts)
        for cls in classes:
            for method_name in ("recall", "recall_detail"):
                with self.subTest(cls=cls.__name__, method=method_name):
                    method = getattr(cls, method_name)
                    params = inspect.signature(method).parameters
                    self.assertIn("stream", params, f"{cls.__name__}.{method_name} 缺少 stream")
                    self.assertIn("game", params, f"{cls.__name__}.{method_name} 缺少 game")


if __name__ == "__main__":
    unittest.main(verbosity=2)
