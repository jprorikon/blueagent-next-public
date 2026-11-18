#!/usr/bin/env python3
"""工具名：模型回的是 **schema 函式名**，registry 登記的是 **工具名**——兩邊都要找得到。

踩到的（2026-10-24）：`memory.search` 的 schema 名是 `memory_search`，
大腦把 tool_call 反查回 schema 名後交給 `ToolRegistry.run()`，而 registry 只認 `memory.search`
⇒ 真模型一叫就 `unknown_tool`（memory_search／weather_get／skill_view／board_feed）。

判據：
  ① bootstrap 註冊的每一支工具，用 schema 名與 registry 名都找得到同一支；
  ② 別名不會蓋掉真名（registry 名優先）；
  ③ 主對話整條：模型叫 `memory_search` ⇒ 工具真的執行（不是 unknown_tool），結果回灌下一輪。
"""

from __future__ import annotations

import json
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.tools import ToolContext, ToolRegistry, ToolResult


def _function_name(tool) -> str:
    return str(((getattr(tool, "schema", None) or {}).get("function") or {}).get("name") or "")


class _Tool:
    def __init__(self, name, function):
        self.name = name
        self.schema = {"type": "function",
                       "function": {"name": function, "description": "x", "parameters": {}}}

    def run(self, ctx, arguments):
        return ToolResult(ok=True, data={"who": self.name})


CTX = ToolContext(entity="mira", principal="owner", thread_id="t", source="app",
                  voice=None, mode="work")


class TestRegistryResolvesSchemaNames(unittest.TestCase):
    def test_every_bootstrap_tool_resolves_by_schema_name(self):
        state = make_state()
        try:
            registry = state.app.tools
            for name in registry.snapshot():
                tool = registry.get(name)
                function = _function_name(tool)
                if not function:
                    continue
                self.assertIs(registry.get(function), tool, f"{function} → {name}")
        finally:
            state.shutdown()

    def test_alias_never_shadows_a_real_name(self):
        registry = (ToolRegistry().register(_Tool("a.b", "c"))
                    .register(_Tool("c", "c")))
        self.assertEqual(registry.run("c", {}, CTX).data["who"], "c")
        self.assertEqual(registry.run("a.b", {}, CTX).data["who"], "a.b")

    def test_unknown_name_is_still_unknown(self):
        result = ToolRegistry().register(_Tool("a.b", "a_b")).run("nope", {}, CTX)
        self.assertEqual(result.error, "unknown_tool:nope")


class TestMainTurnRunsToolBySchemaName(unittest.TestCase):
    def test_model_calling_memory_search_really_runs_it(self):
        state = make_state()

        class LoopBrain:
            def __init__(self):
                self.calls = 0
                self.trace = None

            def run_turn(self, request):
                self.calls += 1
                if self.calls == 1:
                    # 真模型回的是 schema 名（大腦反查消毒名後就是這個）
                    yield ("tool_call", {"id": "call_1", "name": "memory_search",
                                         "arguments": {"query": "小説"}})
                    return
                self.trace = request.meta.get("tool_trace")
                yield ("done", {"text": "うん、覚えてるよ。", "entity": "mira",
                                "principal": "owner", "model": "test"})

        brain = LoopBrain()
        state.app.runtime.ports.brain = brain
        try:
            result = state.send("前に話した小説の件", entity="mira", principal="owner")
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual(brain.calls, 2)
            call = brain.trace[0]["calls"][0]
            self.assertEqual(call["name"], "memory_search", "回灌給模型的名字要跟它叫的一樣")
            content = json.loads(brain.trace[0]["results"][0]["content"])
            self.assertNotIn("unknown_tool", str(content.get("error") or ""), content)
            self.assertTrue(content.get("ok"), content)
        finally:
            state.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
