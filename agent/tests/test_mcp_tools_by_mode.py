#!/usr/bin/env python3
"""MCP 的「逐模式工具白名單」（owner 2026-11-03：她 chat 一輪 16 支工具／9.5k 字）。

判據：
  ① 宣告 `tools_by_mode={"chat": [...]}` ⇒ 聊天裡只放行那幾支；
  ② 沒宣告 ⇒ 照舊全部放行（不影響既有 server）；
  ③ 白名單只影響**可見性**（上下文），執行權限照舊由 `modes`／`owner_only` 管。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.adapter.mcp_tools import MCPTool                     # noqa: E402
from agent.brain.runtime.tools.base import ToolRegistry                # noqa: E402


class _Client:
    last_error = ""

    def call(self, name, arguments):
        return {"ok": True}


def _tool(remote: str, filters: dict | None) -> MCPTool:
    return MCPTool(server="tasks", client=_Client(), remote_name=remote,
                   description="d", schema={"type": "object", "properties": {}},
                   modes=("chat", "work"), on_demand=False,
                   remote_name_filters=filters)


class TestToolsByMode(unittest.TestCase):
    def test_chat_whitelist_hides_the_rest(self):
        registry = ToolRegistry()
        filters = {"chat": ("board", "add")}
        for remote in ("board", "add", "calendar", "state", "note", "ack"):
            registry.register(_tool(remote, filters))
        visible = [name for name in registry.snapshot()
                   if registry.allowed(registry.get(name), mode="chat", is_owner=True)]
        self.assertEqual(sorted(visible), ["mcp_tasks_add", "mcp_tasks_board"])

    def test_work_mode_unaffected(self):
        registry = ToolRegistry()
        filters = {"chat": ("board",)}
        for remote in ("board", "state"):
            registry.register(_tool(remote, filters))
        visible = [name for name in registry.snapshot()
                   if registry.allowed(registry.get(name), mode="work", is_owner=True)]
        self.assertEqual(sorted(visible), ["mcp_tasks_board", "mcp_tasks_state"])

    def test_no_filter_keeps_everything(self):
        registry = ToolRegistry()
        for remote in ("board", "state"):
            registry.register(_tool(remote, None))
        visible = [name for name in registry.snapshot()
                   if registry.allowed(registry.get(name), mode="chat", is_owner=True)]
        self.assertEqual(len(visible), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
