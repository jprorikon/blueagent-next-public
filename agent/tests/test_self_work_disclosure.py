#!/usr/bin/env python3
"""她自己的產出也要守披露層（2026-10-24 review 第 1–3 條）。

判據：
  ① 日記素材只取 public 活動＋主人在場的線；客人 1:1、private 私記都不進；
     日記事實的房間＝素材房間的交集 ⇒ 別的客人在場時召回不到；
  ② 只有 public 活動的日記仍是世界可見（行為不變）；
  ③ 自主任務的工具面是白名單：`memory.search`／`voice.*`／委任不出現，模型硬叫也不執行；
     白名單內的工具用 schema 名（`mcp_firecrawl_firecrawl_search`）叫得到。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.domain.thread import MemoryScope
from agent.brain.runtime.activity_log import PRIVATE, PUBLIC, ActivityLog
from agent.brain.runtime.rawlog import RawLog
from agent.brain.runtime.reflection import DailyReflection
from agent.brain.runtime.tools import ToolContext, ToolRegistry, ToolResult

GUEST_SECRET = "内緒だけど来月転職する"
PRIVATE_NOTE = "誰にも言ってない私だけのメモ"


def _raw_row(at, thread, speaker, room, text, turn):
    return {"at": at, "entity": "mira", "turn": turn, "thread": thread, "role": "user",
            "speaker": speaker, "room": sorted(room), "text": text}


class TestDiaryScope(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.memory = SqliteMemory(self.root / "facts.db", seed=False)
        self.log = ActivityLog(self.root / "logs")
        self.log.append("mira", kind="study", text="規則書を三ページ読んだ", visibility=PUBLIC)
        self.log.append("mira", kind="diary", text=PRIVATE_NOTE, visibility=PRIVATE)
        self.raw = RawLog(self.root / "raw")
        self.now = time.time()

    def _write_raw(self, rows):
        path = self.raw.month_path("mira", self.now)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")

    def _reflect(self, raw=None):
        seen = {}

        def echo(prompt):                     # 最壞情況：模型把素材原樣寫進日記
            seen["prompt"] = prompt
            return prompt[-400:]

        service = DailyReflection(root=self.root / "notes", memory=self.memory,
                                  activity_log=self.log, raw_log=raw, call=echo,
                                  entities=("mira",), host="owner")
        result = service.reflect_once("mira")
        self.assertTrue(result.get("ok"), result)
        return seen["prompt"]

    def _recall(self, *people):
        scope = MemoryScope(principal=people[0], entity="mira", scene="chat",
                            thread_id="th-x")
        return list(self.memory.recall(scope, "転職 三ページ", participants=set(people)))

    def test_guest_one_on_one_and_private_notes_stay_out(self):
        self._write_raw([
            _raw_row(self.now - 60, "th_bob-1", "bob", {"mira", "bob"}, GUEST_SECRET, "t1"),
            _raw_row(self.now - 30, "th_owner-1", "owner", {"mira", "owner"},
                     "今日は転職の話じゃなくて、三ページ読んだって聞いた", "t1"),
        ])
        prompt = self._reflect(self.raw)
        self.assertNotIn(GUEST_SECRET, prompt)
        self.assertNotIn(PRIVATE_NOTE, prompt)
        self.assertIn("三ページ", prompt)

    def test_diary_audience_is_the_rooms_it_came_from(self):
        """日記的**素材**只取主人在場的線；房間是那幾段的交集。

        2026-11-03 review：線路 A（self 永遠想得起來）只放「房間裡只有她」的那幾條。
        日記是從主人在場的對話寫成的、房間裡有主人 ⇒ 照舊走披露閘：主人聽得到，
        客人面前想不起來（不然主人私聊的內容會在 alice 的聊天裡被召回）。
        """
        self._write_raw([
            _raw_row(self.now - 90, "th_group-1", "alice", {"mira", "owner", "alice"},
                     "みんなで規則書の話をした", "t1"),
            _raw_row(self.now - 30, "th_owner-1", "owner", {"mira", "owner"},
                     "あとで三ページ分まとめてね", "t1"),
        ])
        self._reflect(self.raw)
        diary = [row for row in self.memory.snapshot()["facts"] if row.get("label") == "她的日記"]
        self.assertEqual(len(diary), 1, diary)
        self.assertTrue(self._recall("owner"), "主人聽得到")
        self.assertFalse(self._recall("alice"), "素材裡有主人私聊 ⇒ 客人面前想不起來")
        self.assertFalse(self._recall("bob"))

    def test_public_activity_only_diary_stays_world_visible(self):
        self._reflect(raw=None)
        self.assertTrue(self._recall("alice"), "只有 public 活動 ⇒ 跟以前一樣世界可見")


class _Recorder:
    def __init__(self, name, function):
        self.name = name
        self.schema = {"type": "function",
                       "function": {"name": function, "description": "x", "parameters": {}}}
        self.calls = []

    def run(self, ctx, arguments):
        self.calls.append(dict(arguments))
        return ToolResult(ok=True, data={"summary": f"{self.name} ran"})


class _Brain:
    """第一輪依序叫給定的工具，第二輪吐成果。"""

    def __init__(self, names):
        self.names = list(names)
        self.requests = []

    def run_turn(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            for index, name in enumerate(self.names):
                yield ("tool_call", {"id": f"c{index}", "name": name,
                                     "arguments": {"query": "判定"}})
            return
        yield ("done", {"text": "判定の考え方が分かった。"})


class TestSelfWorkWhitelist(unittest.TestCase):
    def setUp(self):
        self.memory_tool = _Recorder("memory.search", "memory_search")
        self.voice_tool = _Recorder("voice.call", "voice.call")
        self.delegate_tool = _Recorder("delegate", "delegate")
        self.search_tool = _Recorder("mcp_firecrawl_firecrawl_search",
                                     "mcp_firecrawl_firecrawl_search")
        self.registry = (ToolRegistry().register(self.memory_tool).register(self.voice_tool)
                         .register(self.delegate_tool).register(self.search_tool))

    def _run(self, names):
        from agent.brain.runtime.self_work import run_step
        brain = _Brain(names)
        result = run_step(brain, self.registry, entity="mira", activity="自習",
                          step="判定を整理", context_factory=lambda: ToolContext(
                              entity="mira", principal="owner", thread_id="self",
                              source="self", voice=None, mode="work",
                              capabilities=frozenset({"delegate"})))
        return brain, result

    def test_only_whitelisted_tools_are_offered(self):
        brain, _ = self._run([])
        offered = {item["function"]["name"] for item in brain.requests[0].meta["tools"]}
        self.assertEqual(offered, {"mcp_firecrawl_firecrawl_search"}, offered)

    def test_forced_calls_outside_whitelist_are_not_executed(self):
        # 就算 context 帶著委任能力，白名單外的工具也不執行
        _, result = self._run(["memory_search", "voice.call", "delegate_hermes",
                               "delegate"])
        self.assertEqual(self.memory_tool.calls, [])
        self.assertEqual(self.voice_tool.calls, [])
        self.assertEqual(self.delegate_tool.calls, [])
        refused = [item for item in result["trace"]
                   if item.get("error") == "not_in_self_work_whitelist"]
        self.assertEqual(len(refused), 4, result["trace"])

    def test_whitelisted_tool_resolves_by_schema_name(self):
        _, result = self._run(["mcp_firecrawl_firecrawl_search"])
        executed = [item for item in result["trace"]
                    if item["name"] == "mcp_firecrawl_firecrawl_search" and item.get("ok")]
        # 這一步沒有查證字眼 ⇒ 沒有預查；這一筆就是模型用 schema 名叫的那次
        self.assertEqual(len(executed), 1, result["trace"])
        self.assertTrue(result["ok"], result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
