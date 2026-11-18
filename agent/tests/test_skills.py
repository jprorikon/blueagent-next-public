#!/usr/bin/env python3
"""工作技能（P4）：索引進 prompt、正文用 `skill.view` 取，權限照 scope／owner_only。

owner 2026-10-23 定的口徑：
  · skill **只放工作技能**，人格不進 skill；
  · `scope: chat` 的技能聊天就能用（guest 也行），`work` 只在委任模式；
  · **只有「委任 Hermes」那類才 owner_only**，不是整庫鎖死。

判據：
  ① frontmatter 解析（name／description／scope／requires／owner_only）；
  ② 可見性：chat 技能 guest 也看得到；work 技能在 chat 模式看不到；owner_only 的 guest 看不到；
  ③ 角色專用版（`data/skills/<entity>/<name>.md`）覆寫同名套件版；
  ④ `skill.view` 工具守住同一條規則（越權回 not_allowed）；
  ⑤ 索引只給「名稱＋一句話」，**不塞正文**（正文按需取）。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.skills import SkillLibrary
from agent.brain.runtime.tools import SkillViewTool, ToolContext

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "agent" / "brain" / "skills"


def ctx(mode: str = "chat", is_owner: bool = True) -> ToolContext:
    return ToolContext(entity="mira", principal="owner" if is_owner else "guest-x",
                       thread_id="th-1", source="app", voice=None,
                       mode=mode, is_owner=is_owner)


class TestSkillLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / "skills"
        (self.root / "weather").mkdir(parents=True)
        (self.root / "weather" / "SKILL.md").write_text(
            "---\nname: weather\ndescription: 問天氣時用\nscope: chat\n"
            "requires: [tool:weather.get]\nowner_only: false\n---\n用 weather.get。\n",
            encoding="utf-8")
        (self.root / "delegate-run").mkdir(parents=True)
        (self.root / "delegate-run" / "SKILL.md").write_text(
            "---\nname: delegate-run\ndescription: 叫 Hermes 去做事\nscope: work\n"
            "owner_only: true\n---\n叫 delegate。\n", encoding="utf-8")
        self.entity_root = pathlib.Path(self.tmp.name) / "entity-skills"
        (self.entity_root / "mira").mkdir(parents=True)
        (self.entity_root / "mira" / "weather.md").write_text(
            "---\nname: weather\ndescription: 她的天氣技能（角色版）\nscope: chat\n---\n"
            "先問地點再查。\n", encoding="utf-8")
        self.lib = SkillLibrary(self.root, entity_root=self.entity_root)

    def test_frontmatter_parsed(self):
        skill = SkillLibrary(self.root).get("weather")   # 只看套件版（角色版沒有 requires）
        self.assertEqual(skill.scope, "chat")
        self.assertEqual(skill.requires, ("tool:weather.get",))
        self.assertFalse(skill.owner_only)

    def test_visibility_by_scope_and_role(self):
        guest_chat = {s.name for s in self.lib.index(mode="chat", is_owner=False)}
        owner_work = {s.name for s in self.lib.index(mode="work", is_owner=True)}
        self.assertIn("weather", guest_chat, "chat 技能 guest 也能用")
        self.assertNotIn("delegate-run", guest_chat, "work 技能不在 chat 出現")
        self.assertIn("delegate-run", owner_work)
        self.assertIn("weather", owner_work, "chat 技能在 work 模式也能用")

    def test_owner_only_hidden_from_guest(self):
        hidden = {s.name for s in self.lib.index(mode="work", is_owner=False)}
        self.assertNotIn("delegate-run", hidden)

    def test_entity_override_wins(self):
        skill = self.lib.get("weather", entity="mira")
        self.assertIn("角色版", skill.description)
        self.assertEqual(skill.source, "entity")

    def test_index_has_no_body(self):
        block = self.lib.render_index(mode="chat", is_owner=True, entity="mira")
        self.assertIn("weather", block)
        self.assertNotIn("先問地點再查", block, "索引不該塞正文")

    def test_view_respects_gate(self):
        skill, reason = self.lib.view("delegate-run", mode="chat", is_owner=False)
        self.assertIsNotNone(skill)
        self.assertEqual(reason, "not_allowed")
        skill, reason = self.lib.view("weather", mode="chat", is_owner=False)
        self.assertEqual(reason, "")


class TestSkillTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / "skills"
        (self.root / "recap").mkdir(parents=True)
        (self.root / "recap" / "SKILL.md").write_text(
            "---\nname: recap\ndescription: 整理回顧\nscope: work\n---\n三步：查、分三塊、寫。\n",
            encoding="utf-8")
        self.lib = SkillLibrary(self.root)
        self.tool = SkillViewTool(self.lib)

    def test_view_returns_body_in_work_mode(self):
        result = self.tool.run(ctx(mode="work"), {"name": "recap"})
        self.assertTrue(result.ok, result.error)
        self.assertIn("三步", result.data["body"])

    def test_view_blocked_in_chat_mode(self):
        result = self.tool.run(ctx(mode="chat"), {"name": "recap"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_allowed")

    def test_missing_skill(self):
        result = self.tool.run(ctx(mode="work"), {"name": "nope"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_found")


class TestBundledSkills(unittest.TestCase):
    def test_weather_and_recap_are_shipped(self):
        lib = SkillLibrary(PACKAGE)
        names = {skill.name for skill in lib.all()}
        self.assertIn("weather", names)
        self.assertIn("session-recap", names)
        weather = lib.get("weather")
        self.assertEqual(weather.scope, "chat", "天氣是聊天就能用的技能")
        recap = lib.get("session-recap")
        self.assertEqual(recap.scope, "work")


if __name__ == "__main__":
    unittest.main(verbosity=2)
