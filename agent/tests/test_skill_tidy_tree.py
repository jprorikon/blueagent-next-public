#!/usr/bin/env python3
"""樹狀維護要進「每週整理」的視野（owner 2026-11-03：「把树状维护这个事情也作为定期技能维护的关注点」）。

判據：
  ① 三人的 `skills-tidy` 都有「先開目次、讀 problems、先修樹」這兩步（不是只挑單條技能）；
  ② MCP 的 `list`（不帶 category）會回 `problems`：分支太多／單支太胖／learned 積壓；
  ③ problems 是**人話**（叫她做什麼），不是一串數字。
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

from agent.brain.runtime.life import PRESETS  # noqa: E402

SERVER = REPO / "agent" / "tools" / "mcp_skills_server.py"


def _front(name: str, category: str) -> str:
    return (f"---\nname: {name}\ndescription: {name}\nscope: work\ncategory: {category}\n---\n"
            "## 完了条件\n- 出る\n\n## 失敗時\n- やり直す\n")


def call(root: pathlib.Path, tool: str, args: dict) -> dict:
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": dict(args, entity="nori")}}
    proc = subprocess.run([sys.executable, str(SERVER)], input=json.dumps(request) + "\n",
                          capture_output=True, text=True,
                          env=dict(os.environ, SKILLS_ROOT=str(root)), timeout=60)
    return json.loads(json.loads(proc.stdout.strip().splitlines()[-1])["result"]["content"][0]["text"])


class TestTidyCoversTree(unittest.TestCase):
    def test_weekly_tidy_steps_include_tree(self):
        for who, items in PRESETS.items():
            tidy = [p for p in items if p.key == "skills-tidy"][0]
            joined = "".join(text for text, _visibility in tidy.steps)
            self.assertIn("problems", joined, f"{who} 的整理活动没看树")
            self.assertIn("目次", joined, f"{who} 的整理活动没先开目次")
            self.assertIn("枝", joined, f"{who} 的整理活动没说枝（樹的分支；prompt 裡要日語）")
            self.assertIn("木", tidy.brief, f"{who} 的 brief 没提「树」")

    def test_list_reports_tree_problems(self):
        root = pathlib.Path(tempfile.mkdtemp()) / "skills"
        (root / "nori").mkdir(parents=True)
        for index in range(9):
            (root / "nori" / f"web-{index}.md").write_text(_front(f"web-{index}", "web"),
                                                          encoding="utf-8")
        for index in range(4):
            (root / "nori" / f"raw-{index}.md").write_text(_front(f"raw-{index}", "learned"),
                                                          encoding="utf-8")
        got = call(root, "list", {})
        problems = " ".join(got["problems"])
        self.assertIn("web", problems)          # 一支 9 条 > 8
        self.assertIn("learned", problems)      # 暂存区积压
        self.assertIn("統合", problems)          # 给的是动作，不是数字

    def test_healthy_tree_has_no_problems(self):
        root = pathlib.Path(tempfile.mkdtemp()) / "skills"
        (root / "nori").mkdir(parents=True)
        (root / "nori" / "one.md").write_text(_front("one", "web"), encoding="utf-8")
        self.assertEqual(call(root, "list", {})["problems"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
