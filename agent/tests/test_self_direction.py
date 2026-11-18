#!/usr/bin/env python3
"""她真的做事了（owner 2026-10-24「四個都做」）。

查到的實況：`make_step_fn` 以前是 `time.sleep(0.65)` + 寫一行模板
「ミラ在『自己查資料…』完成了：翻規則書，找三條以前沒用過的判定」——
沒有任何內容、沒有成品、沒有記憶事實，24 小時跑 12 次全是空轉。

判據：
  A. 一步 = 一次短呼叫，產出真內容 ⇒ 落 `notes/<entity>/<日期>.md` ＋ 一條 `self_note` 事實
  B. 一個活動做完 ⇒ 一句「收穫」事實（驗收：問她「最近學到什麼」答得出來）
  C. 每天一次反思 ⇒ 三行日記檔 ＋ `reflection` 事實；同一天不重寫
  D. 沒有對話素材時，預想改用「她自己在做的事」（以前直接回 not_enough_turns）
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.adapter.memory_sqlite import SqliteMemory  # noqa: E402
from agent.brain.runtime.life import PRESETS, make_step_fn  # noqa: E402
from agent.brain.domain.activity import Activity  # noqa: E402
from agent.brain.domain.entity import CharacterEntity, EntityRegistry  # noqa: E402
from agent.brain.runtime.activity_log import PUBLIC, ActivityLog  # noqa: E402
from agent.brain.runtime.prethink import PreThinker  # noqa: E402
from agent.brain.runtime.reflection import DailyReflection  # noqa: E402


def build_activity(total: int = 2) -> Activity:
    return Activity.create("mira", "自己查資料",
                           [f"步驟{index}" for index in range(total)], kind="study")


def registry() -> EntityRegistry:
    store = EntityRegistry()
    store.register(CharacterEntity(id="mira", name="ミラ", voice="mira"))
    return store


class TestStepProducesContent(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.memory = SqliteMemory(self.root / "facts.db", seed=False)
        self.log = ActivityLog(self.root / "logs")

    def test_step_writes_note_file_and_fact(self):
        step_fn = make_step_fn(self.memory, registry(), 0.0, log=self.log,
                               call=lambda prompt: "判定①：難易度は目標値−2。判定②：失敗でも情報は出る。",
                               notes_root=self.root / "notes")
        activity = build_activity(2)
        for step in activity.steps:
            step_fn(activity, step)
        files = list((self.root / "notes" / "mira").glob("*.md"))
        self.assertEqual(len(files), 1, files)
        body = files[0].read_text(encoding="utf-8")
        self.assertIn("判定①", body, "筆記要有真內容")
        facts = [row["text"] for row in self.memory.snapshot()["facts"]]
        self.assertTrue(any("判定①" in text for text in facts), facts)

    def test_without_call_falls_back_to_template(self):
        step_fn = make_step_fn(self.memory, registry(), 0.0, log=self.log)
        activity = build_activity(1)
        result = step_fn(activity, activity.steps[0])
        self.assertIn("完了", result)
        self.assertFalse(list((self.root / "notes").glob("**/*.md")), "沒呼叫就不該有筆記檔")

    def test_activity_completion_writes_summary(self):
        step_fn = make_step_fn(self.memory, registry(), 0.0, log=self.log,
                               call=lambda prompt: "學到：敘事節奏靠留白。",
                               notes_root=self.root / "notes")
        activity = build_activity(1)
        for step in activity.steps:
            step_fn(activity, step)
        facts = [row["text"] for row in self.memory.snapshot()["facts"]]
        self.assertTrue(any("得たもの" in text for text in facts), facts)


class TestDailyReflection(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.memory = SqliteMemory(self.root / "facts.db", seed=False)
        self.log = ActivityLog(self.root / "logs")
        self.log.append("mira", kind="study", text="背了三條判定", visibility=PUBLIC)

    def reflection(self, call) -> DailyReflection:
        return DailyReflection(root=self.root / "notes", memory=self.memory,
                               activity_log=self.log, call=call,
                               entities=("mira",), clock=lambda: 1792800000.0)

    def test_writes_diary_once_per_day(self):
        calls = []

        def call(prompt: str) -> str:
            calls.append(prompt)
            return "今日は判定の考え方が少し分かった。\n留白が大事だと知った。\n明日は実際に回してみたい。"
        service = self.reflection(call)
        first = service.reflect_once("mira")
        self.assertTrue(first.get("ok"), first)
        second = service.reflect_once("mira")
        self.assertEqual(second.get("reason"), "already_today", "一天只寫一次")
        files = list((self.root / "notes" / "mira").glob("reflection-*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("留白", files[0].read_text(encoding="utf-8"))
        facts = [row["text"] for row in self.memory.snapshot()["facts"]]
        self.assertTrue(any("留白" in text for text in facts), facts)

    def test_no_material_means_no_write(self):
        empty = ActivityLog(self.root / "empty-logs")
        service = DailyReflection(root=self.root / "notes2", memory=self.memory,
                                  activity_log=empty, call=lambda _p: "x",
                                  entities=("mira",), clock=lambda: 1792800000.0)
        self.assertEqual(service.reflect_once("mira")["reason"], "no_material")


class TestPrethinkFromActivity(unittest.TestCase):
    def test_uses_activity_when_no_turns(self):
        root = pathlib.Path(tempfile.mkdtemp())
        log = ActivityLog(root / "logs")
        log.append("mira", kind="study", text="翻了三頁規則書", visibility=PUBLIC)
        log.append("mira", kind="study", text="整理了兩條判定", visibility=PUBLIC)
        seen = {}

        def call(prompt: str) -> str:
            seen["prompt"] = prompt
            return "規則書の判定、まだ全部は覚えてないけど、流れは見えた。"
        thinker = PreThinker(raw_log=None, call=call, root=root / "prethink",
                             entities=("mira",), idle_s=0.0, interval_s=0.0,
                             activity_log=log, clock=lambda: 1792800000.0)
        result = thinker.think_once("mira")
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("source"), "activity")
        self.assertIn("規則書", seen["prompt"])

    def test_no_activity_no_turns_is_clean_failure(self):
        root = pathlib.Path(tempfile.mkdtemp())
        thinker = PreThinker(raw_log=None, call=lambda _p: "x", root=root / "prethink",
                             entities=("mira",), activity_log=ActivityLog(root / "logs"),
                             idle_s=0.0, interval_s=0.0, clock=lambda: 1792800000.0)
        self.assertFalse(thinker.think_once("mira").get("ok"))




class FakeBrain:
    """第一次要工具，第二次吐成果（模擬真的工作迴圈）。"""

    def __init__(self, tool_name: str = "mcp_board_feed") -> None:
        self.tool_name = tool_name
        self.prompts: list[str] = []
        self._round = 0

    def run_turn(self, request):
        self.prompts.append(str(request.text))
        self._round += 1
        if self._round == 1:
            yield ("tool_call", {"id": "c1", "name": self.tool_name,
                                 "arguments": {"query": "判定"}})
            return
        yield ("done", {"text": "判定は目標値−2。失敗しても情報は出る、と分かった。"})


class FakeTool:
    """白名單裡的工具（`mcp_board_feed`）；模型回的是同名 schema 名。"""
    name = "mcp_board_feed"
    schema = {"type": "function", "function": {"name": "mcp_board_feed",
                                              "description": "看公共區的帖", "parameters": {}}}

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, ctx, arguments):
        from agent.brain.runtime.tools import ToolResult
        self.calls.append(dict(arguments))
        return ToolResult(ok=True, data={"summary": "3 件規則筆記"})


class TestSelfWorkUsesTools(unittest.TestCase):
    def test_tool_is_really_executed(self):
        from agent.brain.runtime.self_work import run_step
        from agent.brain.runtime.tools import ToolContext, ToolRegistry

        tool = FakeTool()
        registry = ToolRegistry().register(tool)
        brain = FakeBrain()
        result = run_step(brain, registry, entity="mira", activity="自己查資料",
                          step="翻規則書", context_factory=lambda: ToolContext(
                              entity="mira", principal="owner", thread_id="self",
                              source="self", voice=None, mode="work"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(tool.calls, [{"query": "判定"}], "工具要真的被呼叫")
        self.assertEqual(result["trace"][0]["name"], "mcp_board_feed")
        self.assertIn("判定", result["text"])
        self.assertIn("3 件規則筆記", brain.prompts[-1], "工具結果要回灌給模型")

    def test_no_tool_call_still_returns_text(self):
        from agent.brain.runtime.self_work import run_step
        from agent.brain.runtime.tools import ToolRegistry

        class Plain:
            def run_turn(self, request):
                yield ("done", {"text": "寫了一頁筆記。"})
        result = run_step(Plain(), ToolRegistry(), entity="mira",
                          activity="寫筆記", step="整理")
        self.assertTrue(result["ok"])
        self.assertEqual(result["trace"], [])

    def test_not_configured_is_clean(self):
        from agent.brain.runtime.self_work import run_step
        self.assertFalse(run_step(None, None, entity="mira",
                                  activity="x", step="y")["ok"])



class TestUnverifiedMarking(unittest.TestCase):
    """C：沒有調工具 ⇒ 筆記與事實都要標「未查證」。"""

    def test_note_marks_unverified_when_no_tool_used(self):
        root = pathlib.Path(tempfile.mkdtemp())
        memory = SqliteMemory(root / "facts.db", seed=False)
        log = ActivityLog(root / "logs")

        def work(activity, step, *, entity=""):
            return {"ok": True, "text": "憑印象寫了一點東西。", "trace": []}

        step_fn = make_step_fn(memory, registry(), 0.0, log=log, work=work,
                               notes_root=root / "notes")
        activity = build_activity(1)
        # 降採樣（owner 2026-11-03）之後「每一步寫記憶」預設關閉；
        # 這條測的是「沒用工具的那一步要標未検証」，所以把開關打開再驗。
        os.environ["BLUEAGENT_LIFE_STEP_FACTS"] = "1"
        try:
            for step in activity.steps:
                step_fn(activity, step)
        finally:
            os.environ.pop("BLUEAGENT_LIFE_STEP_FACTS", None)
        body = (root / "notes" / "mira").glob("*.md")
        text = next(iter(body)).read_text(encoding="utf-8")
        self.assertIn("未検証", text)
        facts = [row["text"] for row in memory.snapshot()["facts"]]
        self.assertTrue(any("未検証" in item for item in facts), facts)

    def test_note_shows_tools_when_used(self):
        root = pathlib.Path(tempfile.mkdtemp())
        memory = SqliteMemory(root / "facts.db", seed=False)
        log = ActivityLog(root / "logs")

        def work(activity, step, *, entity=""):
            return {"ok": True, "text": "查到了：命中−2。", "trace": [{"name": "mcp_firecrawl_firecrawl_search"}]}

        step_fn = make_step_fn(memory, registry(), 0.0, log=log, work=work,
                               notes_root=root / "notes")
        activity = build_activity(1)
        for step in activity.steps:
            step_fn(activity, step)
        text = next(iter((root / "notes" / "mira").glob("*.md"))).read_text(encoding="utf-8")
        self.assertIn("mcp_firecrawl_firecrawl_search", text)
        self.assertNotIn("未検証", text)



class TestFailedWorkStep(unittest.TestCase):
    """做作品的一步沒做成 ⇒ 筆記照實寫，但不產生成品檔（日記頁的「作品」不能是空殼）。"""

    def _run(self, result):
        root = pathlib.Path(tempfile.mkdtemp())
        memory = SqliteMemory(root / "facts.db", seed=False)
        step_fn = make_step_fn(memory, registry(), 0.0, log=ActivityLog(root / "logs"),
                               work=lambda activity, step, *, entity="": result,
                               notes_root=root / "notes")
        activity = build_activity(1)
        activity.meta["form"] = "work"
        step_fn(activity, activity.steps[0])
        return root

    def test_failed_step_writes_no_work_file(self):
        root = self._run({"ok": False, "reason": "no_worker"})
        self.assertEqual(list((root / "works" / "mira").glob("*.md")), [])
        text = next(iter((root / "notes" / "mira").glob("*.md"))).read_text(encoding="utf-8")
        self.assertIn("no_worker", text)
        self.assertNotIn("作品：", text)

    def test_finished_step_writes_work_file(self):
        root = self._run({"ok": True, "text": "かさかさと落ち葉。", "trace": []})
        self.assertEqual(len(list((root / "works" / "mira").glob("*.md"))), 1)


class _Search:
    name = "mcp_firecrawl_firecrawl_search"
    schema = {"type": "function", "function": {"name": name, "description": "x",
                                               "parameters": {}}}

    def __init__(self, text):
        self.text = text

    def run(self, ctx, arguments):
        from agent.brain.runtime.tools import ToolResult
        return ToolResult(ok=True, data={"text": self.text})


class TestPrefetch(unittest.TestCase):
    """模型不叫工具時，我們自己先查 Firecrawl（確定性），結果要出現在提示詞裡。"""

    def _run(self, search_text, answer, activity, step):
        from agent.brain.runtime.self_work import run_step
        from agent.brain.runtime.tools import ToolRegistry

        seen = {}

        class Rec:
            def run_turn(self, request):
                seen["prompt"] = str(request.text)
                yield ("done", {"text": answer})

        result = run_step(Rec(), ToolRegistry().register(_Search(search_text)),
                          entity="mira", activity=activity, step=step, persona="ミラ")
        return seen["prompt"], result

    def test_lookup_step_is_prefetched(self):
        prompt, result = self._run("- 部位狙い｜https://x\n  命中ペナルティ −2",
                                   "查到了：部位瞄準 −2。", "自己查資料", "翻規則書，找三條判定")
        self.assertTrue(result["ok"], result)
        self.assertIn("命中ペナルティ", prompt, "查到的資料要進提示詞")
        self.assertEqual(result["trace"][0]["name"], "mcp_firecrawl_firecrawl_search")

    def test_lookup_without_results_says_so(self):
        prompt, result = self._run("", "查不到。", "查資料", "查一篇論文")
        self.assertIn("何も出なかった", prompt)
        self.assertFalse(result["trace"][0]["ok"])

    def test_firecrawl_empty_marker_is_not_evidence(self):
        prompt, result = self._run("（沒有結果）", "查不到。", "查資料", "查一篇論文")
        self.assertIn("何も出なかった", prompt)
        self.assertFalse(result["trace"][0]["ok"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
