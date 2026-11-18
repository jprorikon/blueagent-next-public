#!/usr/bin/env python3
"""遊樂場：自己的 ＋ 共用的（owner 2026-10-22）。

判據：
  ① 每個角色有自己的資料夾；`scope="own"` 由**呼叫者身份**決定，指不到別人那裡；
  ② `shared` 三個人都讀寫得到，寫入會自動附上「誰、什麼時候」；
  ③ 路徑穿越（`../`、絕對路徑）一律拒收；
  ④ 只收純文字、單檔 ≤256 KB。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.playground import MAX_BYTES, Playground
from agent.brain.runtime.tools import PlaygroundTool, ToolContext


def ctx(entity: str) -> ToolContext:
    return ToolContext(thread_id="th-1", entity=entity, principal="owner",
                       mode="chat", is_owner=True, source="", voice=None)


class TestPlayground(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ba-play-"))
        self.play = Playground(self.root)
        for name in ("mira", "aster", "nori"):
            self.play.resolve(name, "own")
        self.play.resolve("", "shared")
        self.tool = PlaygroundTool(self.play)

    def test_each_character_has_her_own_folder(self):
        for name in ("mira", "aster", "nori"):
            self.assertTrue((self.root / name).is_dir())
        self.assertTrue((self.root / "shared").is_dir())

    def test_own_is_private(self):
        self.assertTrue(self.tool.run(ctx("mira"),
                                      {"action": "write", "scope": "own",
                                       "name": "note.md", "content": "這是我的秘密"}).ok)
        # 別人用自己的 own 進去看不到（路徑根本不一樣）
        result = self.tool.run(ctx("aster"), {"action": "read", "scope": "own", "name": "note.md"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_found")
        # 自己看得到
        mine = self.tool.run(ctx("mira"), {"action": "read", "scope": "own", "name": "note.md"})
        self.assertTrue(mine.ok)
        self.assertIn("秘密", mine.data["text"])

    def test_shared_is_writable_and_signed(self):
        self.assertTrue(self.tool.run(ctx("aster"), {"action": "write", "scope": "shared",
                                                   "name": "board.md",
                                                   "content": "明天一起讀書吧"}).ok)
        seen = self.tool.run(ctx("nori"), {"action": "read", "scope": "shared", "name": "board.md"})
        self.assertTrue(seen.ok, seen.error)
        self.assertIn("明天一起讀書吧", seen.data["text"])
        self.assertIn("aster", seen.data["text"], "共用的東西要留名")

    def test_traversal_is_refused(self):
        for bad in ("../escape.md", "/etc/passwd", "sub/dir.md", ".."):
            result = self.tool.run(ctx("mira"), {"action": "write", "scope": "own",
                                                    "name": bad, "content": "x"})
            self.assertFalse(result.ok, bad)

    def test_only_text_and_size_capped(self):
        self.assertFalse(self.tool.run(ctx("mira"), {"action": "write", "scope": "own",
                                                        "name": "a.png",
                                                        "content": "x"}).ok)
        big = "あ" * (MAX_BYTES // 3 + 10)
        result = self.tool.run(ctx("mira"), {"action": "write", "scope": "own",
                                                "name": "big.md", "content": big})
        self.assertFalse(result.ok)
        self.assertIn("too large", result.error)

    def test_list_shows_own_files_only(self):
        self.tool.run(ctx("mira"), {"action": "write", "scope": "own",
                                       "name": "a.md", "content": "1"})
        self.tool.run(ctx("aster"), {"action": "write", "scope": "own",
                                   "name": "b.md", "content": "2"})
        mine = self.tool.run(ctx("mira"), {"action": "list", "scope": "own"})
        names = [row["name"] for row in mine.data["files"]]
        self.assertEqual(names, ["a.md"], names)

    def test_snapshot(self):
        self.tool.run(ctx("nori"), {"action": "write", "scope": "shared",
                                   "name": "x.md", "content": "hi"})
        snapshot = self.play.snapshot(("mira", "aster", "nori"))
        self.assertEqual(snapshot["shared"]["files"], 1)


class TestRegisteredInBootstrap(unittest.TestCase):
    def test_tool_and_folders_exist(self):
        demo = build(data_dir=tempfile.mkdtemp(prefix="ba-play-demo-"),
                     step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        # 按需解鎖（`runtime/tools/channels.py`）：平常不在清單裡，技能 `playground` 載入後才出現
        names = {row["function"]["name"] for row in demo.tools.schemas(mode="chat", is_owner=True)}
        self.assertNotIn("playground", names, names)
        names = {row["function"]["name"] for row in demo.tools.schemas(mode="chat", is_owner=True,
                                                                         unlocked={"playground"})}
        self.assertIn("playground", names, names)
        root = Path(demo.playground.root)
        self.assertTrue((root / "shared").is_dir())
        for entity in ("mira", "aster", "nori"):
            self.assertTrue((root / entity).is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
