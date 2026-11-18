#!/usr/bin/env python3
"""M2 MCP：八工具、环境身份、共享权限核与真实 stdio 子进程。"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.mcp_client import MCPClient
from platform_server import start_platform

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("mcp_board_server", ROOT / "agent" / "tools" / "mcp_board_server.py")
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class BoardMcpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-board-mcp-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "logs").mkdir()
        SERVER.BOARD_ACTOR = "mira"
        # 這幾條測的是公共區本身（不是頻率閘）⇒ 上限關掉，免得多跑幾次就撞 post_daily_limit
        for name in ("POST_PER_DAY", "REPLY_PER_DAY", "LIKE_PER_DAY"):
            old = getattr(SERVER, name)
            setattr(SERVER, name, 0)
            self.addCleanup(setattr, SERVER, name, old)
        self.platform = start_platform(self, [
            ("guest-other", "guest", "guest-other-token"),
        ], entities=[("mira", "mira-token"), ("nori", "nori-token")])
        self.service = SERVER._service()
        self.addCleanup(self.service.close)

    def call(self, name, args):
        reply = SERVER.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args}}, self.service)
        return reply["result"]

    @staticmethod
    def payload(reply):
        return json.loads(reply["content"][0]["text"])

    def test_initialize_and_exactly_eight_tools(self):
        initialized = SERVER.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "board")
        listed = SERVER.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]],
                         ["feed", "get", "post", "reply", "like", "delete",
                          "mood.list", "mood.set"])

    def test_entity_post_and_permissions_share_service_without_activity_writes(self):
        logs = self.root / "logs"
        before = {path: path.stat().st_mtime_ns for path in logs.glob("*.jsonl")}
        created = self.call("post", {"text": "MCP 投稿", "actor": "owner", "entity": "owner"})
        self.assertFalse(created.get("isError"), created)
        post = self.payload(created)["post"]
        self.assertEqual(post["author_id"], "mira")
        self.assertEqual(post["author_kind"], "entity")
        self.assertEqual(post["origin"], "manual")
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in logs.glob("*.jsonl")})
        self.assertFalse(self.call("like", {"id": post["id"]}).get("isError"))
        self.assertFalse(self.call("reply", {"id": post["id"], "text": "公开回复"}).get("isError"))
        detail = self.payload(self.call("get", {"id": post["id"]}))
        self.assertEqual(detail["post"]["replies"], 1)
        self.assertFalse(self.call("delete", {"id": post["id"]}).get("isError"))

    def test_guest_cannot_delete_someone_elses_post(self):
        from agent.adapter.app_board import RemoteBoardService
        from agent.adapter.app_protocol import Actor
        other_service = RemoteBoardService(self.platform.test_base_url, "guest-other-token")
        other = other_service.post(Actor("guest-other", "guest", "别人"), "别人的帖子")["post"]
        denied = self.call("delete", {"id": other["id"]})
        self.assertTrue(denied.get("isError"))
        self.assertIn("denied", denied["content"][0]["text"])

    def test_invalid_environment_identity_is_denied(self):
        SERVER.BOARD_ACTOR = "../owner"
        denied = self.call("feed", {})
        self.assertTrue(denied.get("isError"))
        self.assertIn("invalid_board_actor", denied["content"][0]["text"])

    def test_real_stdio_server_roundtrip(self):
        client = MCPClient(name="board", command=sys.executable,
                           args=[str(ROOT / "agent" / "tools" / "mcp_board_server.py")], cwd=str(ROOT),
                           env={"BOARD_ACTOR": "nori", "BOARD_DATA": str(self.root),
                                "BOARD_POST_PER_DAY": "0", "BOARD_REPLY_PER_DAY": "0",
                                "BOARD_LIKE_PER_DAY": "0"}, timeout_s=10)
        self.addCleanup(client.close)
        tools = client.list_tools()
        self.assertEqual(len(tools), 8)
        created = client.call_tool("post", {"text": "真实 stdio 投稿"})
        self.assertFalse(created.get("isError"), created)
        payload = json.loads(created["content"][0]["text"])
        self.assertEqual(payload["post"]["author_id"], "nori")
        feed = client.call_tool("feed", {"limit": 20})
        self.assertIn("真实 stdio 投稿", feed["content"][0]["text"])

    def test_application_injected_entity_cannot_be_spoofed_by_model_arguments(self):
        from types import SimpleNamespace
        from agent.adapter.mcp_tools import build_tools
        config = self.root / "mcp.json"
        config.write_text(json.dumps({"servers": [{
            "name": "board", "transport": "stdio", "command": sys.executable,
            "args": [str(ROOT / "agent" / "tools" / "mcp_board_server.py")],
            "env": {"BOARD_ACTOR": "from:entity", "BOARD_DATA": str(self.root),
                    "BOARD_POST_PER_DAY": "0", "BOARD_REPLY_PER_DAY": "0",
                    "BOARD_LIKE_PER_DAY": "0"},
            "inject": ["entity"], "modes": ["chat", "work"], "on_demand": False,
        }]}), encoding="utf-8")
        tools, status = build_tools(config, cwd=str(ROOT))
        post = next(tool for tool in tools if tool.name == "mcp_board_post")
        self.addCleanup(post.client.close)
        self.assertEqual(status[0]["tools"], 8)
        self.assertNotIn("entity", post.schema["function"]["parameters"]["properties"])
        result = post.run(SimpleNamespace(entity="nori"), {"text": "隐藏身份", "entity": "owner"})
        self.assertTrue(result.ok, result.error)
        payload = json.loads(result.data["text"])
        self.assertEqual(payload["post"]["author_id"], "nori")


if __name__ == "__main__":
    unittest.main(verbosity=2)
