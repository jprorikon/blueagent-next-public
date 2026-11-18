#!/usr/bin/env python3
"""技能樹狀維護（owner 2026-11-03：「不需要所有請求都一次性讀取全部技能內容」）。

三層：**目次（分支＋代表作）→ 分支展開（skills_list(category=…)）→ 正文（skill.view）**。

判據：
  ① `render_index` 按 category 分組，每組只列前 N 條，其餘寫「ほか M 件」；
  ② 條目一多時，索引字數不會跟著爆（分組上限生效）；
  ③ `skills_list` 不帶 category ⇒ 回分支計數；帶 category ⇒ 只回那一支；
  ④ 她寫的新技能預設落在 `learned` 分支（不會悄悄塞進 web／self）。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.runtime.skills import SkillLibrary  # noqa: E402

SERVER = REPO / "agent" / "tools" / "mcp_skills_server.py"


def _front(name: str, category: str, scope: str = "work") -> str:
    return (f"---\nname: {name}\ndescription: {name} の説明\nscope: {scope}\n"
            f"category: {category}\nrequires: [tool:terminal]\n---\n"
            "## 完了条件\n- 出ること\n\n## 失敗時\n- やり直す\n")


def call(root: pathlib.Path, entity: str, tool: str, args: dict) -> dict:
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": dict(args, entity=entity)}}
    proc = subprocess.run([sys.executable, str(SERVER)], input=json.dumps(request) + "\n",
                          capture_output=True, text=True,
                          env=dict(os.environ, SKILLS_ROOT=str(root)), timeout=60)
    return json.loads(json.loads(proc.stdout.strip().splitlines()[-1])["result"]["content"][0]["text"])


class TestSkillTree(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "skills"
        (self.root / "nori").mkdir(parents=True)
        for index in range(6):
            (self.root / "nori" / f"web-{index}.md").write_text(
                _front(f"web-{index}", "web"), encoding="utf-8")
        (self.root / "nori" / "note-thing.md").write_text(_front("note-thing", "self"), encoding="utf-8")
        self.library = SkillLibrary(None, entity_root=self.root)

    def test_index_groups_and_caps(self):
        text = self.library.render_index(mode="work", entity="nori", per_group=2)
        self.assertIn("[web] 6 件", text)
        self.assertIn("ほか 4 件", text)
        self.assertIn("[self] 1 件", text)
        self.assertEqual(text.count("- [web]"), 1)

    def test_index_stays_small_when_many_skills(self):
        text = self.library.render_index(mode="work", entity="nori", per_group=2)
        self.assertLess(len(text), 400, "索引要維持在「目次」大小")

    def test_list_returns_branches_then_expands(self):
        overview = call(self.root, "nori", "list", {})
        self.assertEqual(overview["branches"], {"web": 6, "self": 1})
        branch = call(self.root, "nori", "list", {"category": "web"})
        self.assertEqual(branch["count"], 6)
        self.assertEqual({row["category"] for row in branch["skills"]}, {"web"})

    def test_new_skill_defaults_to_learned(self):
        call(self.root, "nori", "write", {"name": "brand-new", "description": "x",
                                         "body": "## 完了条件\n- ok\n\n## 失敗時\n- retry\n"})
        got = call(self.root, "nori", "view", {"name": "brand-new"})
        self.assertEqual(got["status"], "trial")
        listed = call(self.root, "nori", "list", {"category": "learned"})
        self.assertEqual([row["name"] for row in listed["skills"]], ["brand-new"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
