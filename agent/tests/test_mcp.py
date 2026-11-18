#!/usr/bin/env python3
"""MCP（P5）：外部能力走協議接進來，且**壞掉不擋對話**。

判據：
  ① 客戶端能跑完 `initialize` → `tools/list` → `tools/call`（用一個假 server 驗）；
  ② 工具名消毒成 `mcp_<server>_<tool>`（DeepSeek 不收帶點的名字）；
  ③ `data/mcp.json` 的 `modes` / `owner_only` 真的影響能不能用（跟技能同一套口徑）；
  ④ server 掛掉 ⇒ `MCPError` 轉成 `ok=False`，不往外拋；
  ⑤ 連不上時那批工具不存在（不是每次呼叫才失敗），啟動狀態可回報。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import textwrap
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.mcp_client import MCPClient
from agent.adapter.mcp_tools import MCPTool, build_tools, tool_name_for
from agent.brain.runtime.tools import ToolContext

FAKE_SERVER = textwrap.dedent('''
    import json, sys
    TOOLS = [{"name": "echo.text", "description": "回聲",
              "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                              "required": ["text"]}}]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg.get("params") or {}
            result = {"content": [{"type": "text",
                                   "text": "echo:" + str((params.get("arguments") or {}).get("text"))}]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
        sys.stdout.flush()
''')


def ctx(is_owner: bool = True, mode: str = "chat") -> ToolContext:
    return ToolContext(entity="mira", principal="owner" if is_owner else "guest-x",
                       thread_id="th-1", source="app", voice=None,
                       mode=mode, is_owner=is_owner)


class TestMCPClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = pathlib.Path(self.tmp.name) / "fake_server.py"
        self.server.write_text(FAKE_SERVER, encoding="utf-8")

    def client(self) -> MCPClient:
        client = MCPClient(name="fake", command=sys.executable, args=[str(self.server)],
                           timeout_s=10)
        self.addCleanup(client.close)
        return client

    def test_roundtrip(self):
        client = self.client()
        tools = client.list_tools()
        self.assertEqual([tool["name"] for tool in tools], ["echo.text"])
        result = client.call_tool("echo.text", {"text": "hi"})
        self.assertEqual(result["content"][0]["text"], "echo:hi")

    def test_missing_command_is_error_not_crash(self):
        client = MCPClient(name="nope", command="/definitely/not/here")
        self.assertEqual(client.list_tools(), [])
        self.assertTrue(client.last_error)

    def test_config_builds_prefixed_tools(self):
        config = pathlib.Path(self.tmp.name) / "mcp.json"
        config.write_text(json.dumps({"servers": [
            {"name": "fake", "transport": "stdio", "command": sys.executable,
             "args": [str(self.server)], "modes": ["chat"], "owner_only": False,
             "timeout_s": 10}]}), encoding="utf-8")
        tools, status = build_tools(config)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "mcp_fake_echo_text")
        self.assertEqual(tools[0].schema["function"]["name"], "mcp_fake_echo_text")
        self.assertEqual(status[0]["tools"], 1)

    def test_unreachable_server_yields_no_tools(self):
        config = pathlib.Path(self.tmp.name) / "mcp2.json"
        config.write_text(json.dumps({"servers": [
            {"name": "dead", "command": "/definitely/not/here"}]}), encoding="utf-8")
        tools, status = build_tools(config)
        self.assertEqual(tools, [])
        self.assertTrue(status[0]["error"])

    def test_missing_config_is_fine(self):
        tools, status = build_tools(pathlib.Path(self.tmp.name) / "absent.json")
        self.assertEqual((tools, status), ([], []))


class TestMCPToolGate(unittest.TestCase):
    def setUp(self):
        client = MCPClient(name="fake", command="/definitely/not/here")
        self.tool = MCPTool(server="fake", client=client, remote_name="echo.text",
                            description="回聲", schema={"type": "object"},
                            modes=("chat",), owner_only=False)

    def test_name_is_sanitised(self):
        self.assertEqual(tool_name_for("firecrawl", "firecrawl.scrape"),
                         "mcp_firecrawl_firecrawl_scrape")

    def test_failure_becomes_not_ok(self):
        result = self.tool.run(ctx(), {"text": "hi"})
        self.assertFalse(result.ok)
        self.assertIn("mcp_error", result.error)

    def test_modes_and_owner_gate(self):
        from agent.brain.runtime.tools import ToolRegistry
        registry = ToolRegistry().register(self.tool)
        self.assertTrue(registry.allowed(self.tool, mode="chat", is_owner=False))
        self.assertFalse(registry.allowed(self.tool, mode="work", is_owner=True),
                         "modes 只給 chat ⇒ work 不該放行")
        owner_only = MCPTool(server="fake", client=self.tool.client, remote_name="x",
                             description="", schema={}, modes=("chat",), owner_only=True)
        self.assertFalse(registry.allowed(owner_only, mode="chat", is_owner=False))
        self.assertTrue(registry.allowed(owner_only, mode="chat", is_owner=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
