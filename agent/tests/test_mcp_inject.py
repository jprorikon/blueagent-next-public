#!/usr/bin/env python3
"""MCP 工具的參數注入（owner 2026-11-03）：

外置的 `tasks` MCP server 不知道「現在是誰」，由應用側把 `ctx.entity` 注入。
判據：
  ① 設了 `inject=["entity"]` ⇒ 呼叫時把當前角色塞進 arguments；
  ② 模型自己傳的 entity 會被**覆蓋**（不可偽造別人的台帳）；
  ③ 沒設 inject 的 server（firecrawl）行為不變。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.adapter.mcp_tools import MCPTool  # noqa: E402


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, dict(arguments)))
        return {"content": [{"type": "text", "text": "ok"}]}


def _tool(*, inject=()) -> tuple[MCPTool, _Client]:
    client = _Client()
    tool = MCPTool(server="tasks", client=client, remote_name="board",
                   description="board", schema={"type": "object", "properties": {}},
                   modes=("chat",), on_demand=False, inject=inject)
    return tool, client


class TestEntityInjection(unittest.TestCase):
    def test_entity_is_injected(self):
        tool, client = _tool(inject=("entity",))
        ctx = SimpleNamespace(entity="mira")
        tool.run(ctx, {"per_column": 5})
        self.assertEqual(client.calls[0][1], {"per_column": 5, "entity": "mira"})

    def test_model_cannot_spoof_entity(self):
        tool, client = _tool(inject=("entity",))
        ctx = SimpleNamespace(entity="mira")
        tool.run(ctx, {"entity": "aster"})          # 模型亂填別人的名字
        self.assertEqual(client.calls[0][1]["entity"], "mira")

    def test_missing_entity_refuses_instead_of_trusting_the_model(self):
        tool, client = _tool(inject=("entity",))
        result = tool.run(SimpleNamespace(entity=""), {"entity": "aster"})
        self.assertFalse(result.ok)
        self.assertEqual(client.calls, [], "給不出當前角色就不該把模型填的送出去")

    def test_server_rejects_path_like_entity(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mcp_tasks_server", pathlib.Path(__file__).resolve().parents[2] / "agent" / "tools" / "mcp_tasks_server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        reply = server._call("board", {"entity": "../../etc/x"})
        self.assertTrue(reply.get("isError"))

    def test_without_inject_nothing_is_added(self):
        tool, client = _tool()
        tool.run(SimpleNamespace(entity="mira"), {"url": "x"})
        self.assertEqual(client.calls[0][1], {"url": "x"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
