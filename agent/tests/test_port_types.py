#!/usr/bin/env python3
"""端口型別（2026-10-25 review P2）：應用層只認協議，不認具體實作。

判據：
  ① `app/ports.py`、`app/pipeline.py`、`runtime/life.py` 不再 import 具體後端
     （`brain.mock`、`memory.in_memory`）——以前型別標成 InMemoryMemory／MockBrain，契約是錯的；
  ② 三種記憶後端都實作 `ProbeMemoryPort` 宣告的全部方法；兩種大腦都實作 `BrainPort`；
     記憶後端也都能當 `FactSinkPort`（有 `add`）。方法清單直接從協議本身讀，協議改了這裡跟著變。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_hindsight import HindsightMemory
from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.mock import MockBrain
from agent.brain.openai_compat import OpenAICompatibleBrain
from agent.brain.memory.in_memory import InMemoryMemory
from agent.brain.ports import BrainPort, FactSinkPort, ProbeMemoryPort

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONCRETE = {"agent.brain.mock", "agent.brain.memory.in_memory"}


def _protocol_methods(protocol) -> set[str]:
    names: set[str] = set()
    for klass in protocol.__mro__:
        if klass is object or not getattr(klass, "_is_protocol", False):
            continue
        names |= {name for name, value in vars(klass).items()
                  if callable(value) and not name.startswith("_")}
    return names


class TestAppDependsOnProtocols(unittest.TestCase):
    def test_no_concrete_backend_imports(self):
        hits = []
        for rel in ("agent/brain/compose/ports.py", "agent/brain/compose/pipeline.py",
                    "agent/brain/runtime/life.py"):
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in CONCRETE:
                    hits.append(f"{rel}:{node.lineno} {node.module}")
        self.assertEqual(hits, [])


class TestImplementationsSatisfyProtocols(unittest.TestCase):
    def test_protocols_are_not_empty(self):
        self.assertEqual(_protocol_methods(ProbeMemoryPort),
                         {"recall", "recall_detail", "record", "probe"})
        self.assertEqual(_protocol_methods(BrainPort), {"run_turn"})

    def test_every_memory_backend_implements_the_port(self):
        for backend in (InMemoryMemory, SqliteMemory, HindsightMemory):
            for protocol in (ProbeMemoryPort, FactSinkPort):
                missing = sorted(name for name in _protocol_methods(protocol)
                                 if not callable(getattr(backend, name, None)))
                self.assertEqual(missing, [], f"{backend.__name__} 缺 {protocol.__name__} 的方法")

    def test_every_brain_implements_the_port(self):
        for brain in (MockBrain, OpenAICompatibleBrain):
            self.assertTrue(callable(getattr(brain, "run_turn", None)), brain.__name__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
