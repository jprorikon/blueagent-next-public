#!/usr/bin/env python3
"""工具面按交流對象與通話渠道收窄（owner 2026-11-04）。"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.adapter.mcp_tools import build_tools
from agent.brain.runtime.tools import ToolRegistry, ToolResult
from agent.brain.runtime.tools.sticker import StickerSendTool
from agent.brain.runtime.tools.voice import VoiceHangupTool


class _Tool:
    def __init__(self, name, **attrs):
        self.name = name
        self.schema = {"type": "function", "function": {"name": name.replace(".", "_")}}
        for key, value in attrs.items():
            setattr(self, key, value)

    def run(self, ctx, arguments):
        return ToolResult(ok=True)


def _names(registry, **kwargs):
    return [row["function"]["name"] for row in registry.schemas(**kwargs)]


class TestToolFaceTrim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = pathlib.Path(self.tmp.name) / "mcp.json"
        config.write_text(json.dumps({"servers": [
            {"name": "tasks", "command": "unused", "owner_only": False, "on_demand": False},
            {"name": "skills", "command": "unused", "owner_only": False, "on_demand": False},
        ]}), encoding="utf-8")
        rows = [
            {"name": "board", "description": "board", "inputSchema": {"type": "object"}},
            {"name": "write", "description": "write", "inputSchema": {"type": "object"}},
            {"name": "trial", "description": "trial", "inputSchema": {"type": "object"}},
            {"name": "retire", "description": "retire", "inputSchema": {"type": "object"}},
            {"name": "view", "description": "view", "inputSchema": {"type": "object"}},
        ]
        with mock.patch("agent.adapter.mcp_tools.MCPClient.list_tools", return_value=rows):
            tools, _ = build_tools(config)
        self.registry = ToolRegistry()
        for name in ("memory.search", "memory.open", "skill.view", "sticker.send"):
            self.registry.register(_Tool(name))
        self.registry.register(VoiceHangupTool())
        for tool in tools:
            self.registry.register(tool)

    def test_guest_does_not_get_task_or_skill_management_tools(self):
        names = _names(self.registry, source="phone:guest-x", is_owner=False)
        self.assertFalse(any(name.startswith("mcp_tasks_") for name in names), names)
        for name in ("mcp_skills_write", "mcp_skills_trial", "mcp_skills_retire"):
            self.assertNotIn(name, names)

    def test_call_has_only_hangup_and_memory_search(self):
        for source in ("phone:call", "mr:call", "voice:desk", "mic:desk"):
            names = _names(self.registry, source=source, is_owner=True,
                           unlocked=self.registry.snapshot())
            self.assertEqual(set(names), {"voice.hangup", "memory_search"}, (source, names))

    def test_owner_app_keeps_core_tools_and_skills_view_is_on_demand(self):
        names = _names(self.registry, source="phone:owner", is_owner=True)
        self.assertIn("mcp_tasks_board", names)
        self.assertIn("skill_view", names)
        self.assertIn("sticker_send", names)
        self.assertNotIn("mcp_skills_view", names)
        unlocked = _names(self.registry, source="phone:owner", is_owner=True,
                          unlocked={"mcp_skills_view"})
        self.assertIn("mcp_skills_view", unlocked)

    def test_sticker_schema_has_a_real_description(self):
        sticker = SimpleNamespace(id="smile", meaning="にっこり")
        catalog = SimpleNamespace(list=lambda entity: [sticker])
        schema = StickerSendTool(catalog).schema_for(entity="mira", source="phone:owner")
        self.assertTrue(schema["function"]["description"].strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
