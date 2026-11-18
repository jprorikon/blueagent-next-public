#!/usr/bin/env python3
"""主動訊息也要有人格（owner 2026-10-24）。

實測：她開始用「俺」自稱。追下去發現人格檔 `agent/persona/mira.md` 明明寫著
「一人称は『私』」，錯的是**主動訊息那條路**——`_compose_heartbeat()` 建 BrainRequest
時沒有帶 `system`（人格是 PersonaStage 才貼進 main chat 的），於是主動訊息是在
「完全沒有角色設定」的情況下生成的，模型的預設日語第一人稱就是「俺」。

判據：
  ① `heartbeat_system()` 一定帶上「一人称は『私』／相手は『オーナー』」的硬性提醒
  ② 有 soul 檔／persona core 就一起併進去（順序：人格 → 肖像 → 提醒）
  ③ 什麼都沒給也不能回空字（那等於回到沒有角色的狀態）
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.compose.bootstrap import heartbeat_system  # noqa: E402


class TestHeartbeatSystem(unittest.TestCase):
    def test_always_pins_first_person_and_address(self):
        body = heartbeat_system()
        self.assertIn("一人称は「私」", body)
        self.assertIn("オーナー", body)

    def test_includes_soul_and_core_blocks(self):
        body = heartbeat_system("SOUL 段落", "CORE 段落")
        self.assertIn("SOUL 段落", body)
        self.assertIn("CORE 段落", body)
        self.assertLess(body.index("SOUL 段落"), body.index("CORE 段落"))
        self.assertLess(body.index("CORE 段落"), body.index("一人称は「私」。相手は"))

    def test_persona_file_really_says_private_first_person(self):
        """人格檔本身也要釘住一人稱（不然只有提醒那一行在撐）。"""
        soul = pathlib.Path(__file__).resolve().parent.parent.parent / "agent" / "persona" / "mira.md"
        if not soul.exists():
            self.skipTest("沒有 soul 檔")
        text = soul.read_text(encoding="utf-8")
        self.assertIn("一人称は「私」", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
