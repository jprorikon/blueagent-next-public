#!/usr/bin/env python3
"""工具回合用完、她卻還沒吐出字 ⇒ 最後再要一句「報告」（owner 2026-11-08）。

實測：主人在 Discord 私聊說「観測記録、ファイルで送って」，她三個工具回合都在 `terminal`
裡找檔案（`ls notes/ works/` 全部 exit 2），`finish=tool_calls`、正文 0 字 ⇒ 端末什麼都沒收到。
以前 `MAX_ROUNDS` 用完就是靜默結束——主人只覺得「她沒反應」。

判據：
  A. 工具回合用完且沒有正文 ⇒ **多發一次**請求，`tools=[]` 且 `[[tool_limit]]`，取那一輪的正文
  B. 那一輪不再給工具（否則她又會繼續找檔案）
  C. 收尾那一問失敗 ⇒ 仍是空字（不能因此炸整輪）
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

import demo_common  # noqa: E402,F401
from agent.brain.compose.state import AppState  # noqa: E402


class TestToolLimitReport(unittest.TestCase):
    def setUp(self):
        import os
        os.environ.setdefault("BLUEAGENT_BRAIN", "mock")
        self.data_dir = tempfile.mkdtemp(prefix="ba-tool-limit-")
        self.app = demo_common.build(brain_delay=0.0, data_dir=self.data_dir,
                                     step_duration=0.02, life_pause=0.005)
        self.state = AppState(self.app)

    def _wrap(self, always_call: bool, final_text: str = "ファイルは見つからなかったわ。パスを教えて。"):
        """前三輪都只叫工具；第四輪（tools=[]）才給正文。"""
        seen: list[list] = []

        def run_turn(request):
            tools = list(request.meta.get("tools") or [])
            seen.append(tools)
            if always_call or tools:
                yield "tool_call", {"id": "c%d" % len(seen), "name": "memory_search",
                                    "arguments": {"query": "観測"}}
                yield "done", {"text": ""}
                return
            yield "delta", {"text": final_text}
            yield "done", {"text": final_text}

        self.app.brain.run_turn = run_turn
        return seen

    def test_final_request_has_no_tools_and_its_text_is_used(self):
        seen = self._wrap(always_call=False)
        result = self.state.turns.run("観測記録、ファイルで送って", entity="aster", principal="owner")
        self.assertEqual(demo_common.done_text(result), "ファイルは見つからなかったわ。パスを教えて。")
        # 前幾輪有工具、最後一輪一定是空的（不准再叫她用道具）
        self.assertTrue(any(seen[:-1]))
        self.assertEqual(seen[-1], [])

    def test_failure_of_the_final_request_still_returns(self):
        def broken(request):
            if request.meta.get("tools"):
                yield "tool_call", {"id": "c", "name": "memory_search", "arguments": {"query": "x"}}
                yield "done", {"text": ""}
                return
            yield "error", {"error": "network_error", "text": "boom"}

        self.app.brain.run_turn = broken
        result = self.state.turns.run("観測記録、ファイルで送って", entity="aster", principal="owner")
        self.assertEqual(demo_common.done_text(result), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
