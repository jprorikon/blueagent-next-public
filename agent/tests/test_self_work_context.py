#!/usr/bin/env python3
"""自主工作時的「上下文夠不夠」四條（owner 2026-11-03）：

  ① 讀筆記的窗口：3 → 7 → **10 天**（她自己做事沒有回話延遲壓力，可以往深给）；
  ② 筆記 digest：400 → 800 → **1600 字**（讀進來的是段落，400 字只夠看兩段）；
  ③ **進度**：prompt 要寫「第幾步／共幾步」（重啟或隔幾小時續做時，她原本不知道走到哪）；
  ④ 歷史從 `step.note` **落檔重建**（記憶體那份一重啟就空）。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.runtime.self_work import _digest  # noqa: E402


class _FakeTools:
    """只回一份筆記的工具面；記下被要求的參數。

    要假裝成 registry：`get(name)` 回帶 `schema` 的工具、`allowed(...)` 放行，
    不然 `_allowed_tools` 會把 `notes.read` 濾掉，預取根本不會發生。
    """

    def __init__(self, *, lines: list[str]) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.lines = lines
        note_schema = {"type": "function",
                       "function": {"name": "notes_read", "description": "自己的筆記",
                                    "parameters": {"type": "object", "properties": {}}}}
        self._tools = {"notes.read": SimpleNamespace(name="notes.read", schema=note_schema)}

    def get(self, name):
        return self._tools.get(name)

    def allowed(self, tool, *, mode="", is_owner=True):
        return True

    def run(self, name, arguments, context):
        self.calls.append((name, dict(arguments)))
        if name == "notes.read":
            return SimpleNamespace(ok=True, error="", data={"count": len(self.lines),
                                                            "lines": self.lines})
        return SimpleNamespace(ok=True, error="", data={})


class TestNotesWindowAndDigest(unittest.TestCase):
    def test_digest_can_use_the_wider_budget(self):
        long_text = "あ" * 900
        result = SimpleNamespace(ok=True, error="", data={"text": long_text})
        self.assertEqual(len(_digest(result, max_len=800)), 800)
        self.assertEqual(len(_digest(result)), 400)      # 其他工具的預設不變

    def test_note_step_prefetches_deep_window(self):
        from agent.brain.runtime.self_work import run_step
        tools = _FakeTools(lines=["2026-11-01 のノート段落", "2026-11-02 のノート段落"])
        captured: dict = {}

        class _Brain:
            def run_turn(self, request):
                captured["prompt"] = str(request.text or "")
                yield "done", {}

        run_step(_Brain(), tools, entity="mira", activity="数日前のノートを読み返す",
                 step="自分のノートを読む", form="note",
                 context_factory=lambda: object())
        notes_call = next(call for call in tools.calls if call[0] == "notes.read")
        self.assertEqual(notes_call[1], {"days": 14, "limit": 24})
        self.assertIn("自分のノート", captured["prompt"])


class TestProgressLine(unittest.TestCase):
    def test_progress_is_rendered(self):
        from agent.brain.runtime.self_work import run_step
        tools = _FakeTools(lines=[])
        captured: dict = {}

        class _Brain:
            def run_turn(self, request):
                captured["prompt"] = str(request.text or "")
                yield "done", {}

        run_step(_Brain(), tools, entity="mira", activity="ルールを読む",
                 step="判定の章を読む", progress="2/4ステップ目",
                 context_factory=lambda: object())
        self.assertIn("この活動の進み具合", captured["prompt"])
        self.assertIn("2/4ステップ目", captured["prompt"])


class TestHistoryFromDisk(unittest.TestCase):
    def test_persisted_step_notes_are_used_when_memory_is_empty(self):
        from agent.brain.domain.activity import Activity
        from agent.brain.runtime.life_step import _content_of

        activity = Activity.create("mira", "ノートを読み返す", ["読む", "写す", "まとめる"])
        from agent.brain.domain.activity import StepStatus
        activity.steps[0].note = "完了：二段落だけ読めた"
        activity.steps[0].status = StepStatus.DONE
        seen: dict = {}

        def work(title, step, *, entity="", history=(), progress=""):
            seen["history"] = list(history)
            seen["progress"] = progress
            return {"ok": True, "text": "まとめた", "trace": []}

        _content_of(activity, "ミラ", "まとめる", work=work,
                    history=[], progress="3/3ステップ目")
        self.assertTrue(any("二段落だけ読めた" in item for item in seen["history"]))
        self.assertEqual(seen["progress"], "3/3ステップ目")


if __name__ == "__main__":
    unittest.main(verbosity=2)
