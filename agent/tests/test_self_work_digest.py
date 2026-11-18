#!/usr/bin/env python3
"""工具結果要**真的回灌**給模型（owner 2026-10-24 第③項的實測追加）。

實測鏈路：`mcp_board_feed` 真的有回內容（count>0），但 `self_work` 只讀 `summary`／`text`
兩個 key，而這個工具回的是 `{"entity","lines","count"}` ⇒ 模型看到的是空白 ⇒
她照樣寫「查不到」（aster 一天 266 步空轉）。判據：

  ① `lines` 形狀的結果要進到 prompt；
  ② `summary`／`text` 照舊；
  ③ 工具失敗時把 error 餵回去（模型要知道是失敗，不是「沒有東西」）。
"""

from __future__ import annotations

import pathlib
import tempfile

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.activity_log import PUBLIC, ActivityLog
from agent.brain.runtime.self_work import _digest, run_step
from agent.brain.runtime.tools import ToolContext, ToolRegistry, ToolResult


class _LinesTool:
    """回 `lines` 形狀的假工具（以前的 `activity.peek`；2026-11-07 廢除後改用假件守同一條判據）。"""

    name = "mcp_board_feed"

    schema = {"type": "function", "function": {
        "name": "mcp_board_feed", "description": "x", "parameters": {}}}

    def __init__(self, rows):
        self.rows = list(rows)

    def run(self, ctx, arguments):
        return ToolResult(ok=True, data={"count": len(self.rows), "lines": self.rows})


class _Brain:
    """第一輪叫一個工具，第二輪吐成果；把收到的 prompt 記下來。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.prompts: list[str] = []

    def run_turn(self, request):
        self.prompts.append(str(getattr(request, "text", "") or ""))
        if len(self.prompts) == 1:
            yield ("tool_call", {"id": "c1", "name": self.name, "arguments": {"entity": "nori"}})
            return
        yield ("done", {"text": "記録を読んだ。"})


class _Failing:
    name = "boom.tool"

    schema = {"type": "function", "function": {"name": "boom_tool", "description": "x",
                                               "parameters": {}}}

    def run(self, ctx, arguments):
        return ToolResult(ok=False, error="connection refused")


class TestDigest(unittest.TestCase):
    def test_lines_shape_reaches_prompt(self):
        log = ActivityLog(pathlib.Path(tempfile.mkdtemp(prefix="ba-dig-")))
        log.append("nori", kind="play", text="ノリは新作を選んだ", visibility=PUBLIC,
                   activity="逛商店頁", step=0, activity_id="a1")
        tools = ToolRegistry().register(_LinesTool(["ノリは新作を選んだ"]))
        brain = _Brain("mcp_board_feed")
        result = run_step(brain, tools, entity="aster", activity="觀測",
                          step="讀ノリ的記錄",
                          context_factory=lambda: ToolContext(
                              entity="aster", principal="owner", thread_id="self",
                              source="self", voice=None, mode="work"))
        self.assertTrue(result["ok"], result)
        self.assertIn("ノリは新作を選んだ", brain.prompts[-1],
                      "工具結果（lines）必須進到 prompt，否則模型只會寫查不到")

    def test_public_step_tells_her_the_text_will_be_read_by_others(self):
        """owner 2026-11-08：公開那一步要讓她知道「會被別人看到」，不是自言自語。"""
        brain = _Brain("mcp_board_feed")
        tools = ToolRegistry().register(_LinesTool([]))
        ctx = lambda: ToolContext(entity="aster", principal="owner", thread_id="self",
                                  source="self", voice=None, mode="work")
        run_step(brain, tools, entity="aster", activity="観測", step="まとめる",
                 context_factory=ctx, public=True)
        self.assertIn("公開の記録", brain.prompts[0])
        brain_private = _Brain("mcp_board_feed")
        run_step(brain_private, tools, entity="aster", activity="観測", step="まとめる",
                 context_factory=ctx, public=False)
        self.assertNotIn("公開の記録", brain_private.prompts[0])

    def test_posting_is_framed_as_social_behaviour_not_task_output(self):
        """owner 2026-11-08：「想发了就发」——发帖是社交行为，不是任务的延续。"""
        from agent.brain.runtime.self_work import POST_NOTE
        self.assertIn("言いたくなったら", POST_NOTE)
        self.assertIn("反応がほしい", POST_NOTE)
        self.assertNotIn("成果があったら", POST_NOTE, "不要再写成「有成果才发」")
        self.assertIn("活動の途中でも", POST_NOTE, "不必等活動收束")

    def test_failure_error_reaches_prompt(self):
        result = _digest(_Failing().run(None, {}))
        self.assertIn("connection refused", result)

    def test_summary_shape_still_works(self):
        class _Ok:
            ok = True
            data = {"summary": "三條判定"}
            error = ""
        self.assertEqual(_digest(_Ok()), "三條判定")

    def test_unknown_shape_is_json(self):
        class _Ok:
            ok = True
            data = {"count": 2, "tags": ["a", "b"]}
            error = ""
        self.assertIn("\"count\": 2", _digest(_Ok()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
