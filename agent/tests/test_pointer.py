#!/usr/bin/env python3
"""門牌（pointer）：摘要 → 原文的指針，以及能吃掉它的那扇門（`memory.open`）。

判據（借自 `cyanwell/dsh-agent-soul` 的 ECHO「門牌」紀律，2026-10-25）：
  ① 拿得到精確位置才寫門牌；拿不到回空字串（**不寫半個門牌**）；
  ② 打不開就回 `turn_not_found`，**不拿鄰行充數**（錯的門牌比沒有更壞）；
  ③ `open` 必須吃得下 `search` 自己吐的 `ptr`——否則門牌只是一串好看的代號；
  ④ 披露規則不因「回原文」而放寬（在場的人都在房間裡才給）。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.domain.thread import ThreadKey, TurnRecord
from agent.brain.runtime.memory import pointer as ptr_mod
from agent.brain.runtime.memory.memory_search import MemorySearch
from agent.brain.runtime.rawlog import RawLog
from agent.brain.runtime.tools import MemoryOpenTool, ToolContext
from agent.brain.runtime.memory.window_store import WindowStore


class TestMake(unittest.TestCase):
    def test_single_turn(self):
        self.assertEqual(ptr_mod.make("mira", ["t_abc"]), "raw:mira#t=t_abc")

    def test_window_counts_turns(self):
        self.assertEqual(ptr_mod.make("mira", ["t_a", "t_b", "t_c"]),
                         "raw:mira#t=t_a+3")

    def test_no_turn_ids_means_no_pointer(self):
        self.assertEqual(ptr_mod.make("mira", []), "")
        self.assertEqual(ptr_mod.make("mira", ["", None]), "")
        self.assertEqual(ptr_mod.make("", ["t_a"]), "")

    def test_null_ids_never_become_a_pointer(self):
        """★ 實測到的坑：舊產物寫出 `turn_ids: [null, null, …]`，`str(None)` ＝ "None"
        ⇒ 會造出 `raw:mira#t=None+12` 這種**打不開的假門牌**。必須當成「沒有 id」。"""
        self.assertEqual(ptr_mod.make("mira", [None, None, None]), "")
        self.assertEqual(ptr_mod.make("mira", ["None"]), "")
        self.assertEqual(ptr_mod.make("mira", ["null", "undefined"]), "")
        self.assertEqual(ptr_mod.make("mira", [None, "t_a", None, "t_b"]),
                         "raw:mira#t=t_a+2")

    def test_repeated_ids_count_once(self):
        """提煉器按原文行傳 id（一輪兩行）、封片按 TurnRecord 傳——`+n` 都要是輪數。"""
        self.assertEqual(ptr_mod.make("mira", ["t_a", "t_a", "t_b", "t_b"]),
                         "raw:mira#t=t_a+2")
        self.assertEqual(ptr_mod.make("mira", ["t_a", "t_a"]), "raw:mira#t=t_a")

    def test_parse_roundtrip(self):
        parsed = ptr_mod.parse("raw:mira#t=t_a+5")
        self.assertEqual(parsed["entity"], "mira")
        self.assertEqual(parsed["turn"], "t_a")
        self.assertEqual(parsed["count"], 5)
        self.assertIsNone(ptr_mod.parse("mira#t=t_a"))
        self.assertIsNone(ptr_mod.parse("raw:mira"))
        self.assertIsNone(ptr_mod.parse(""))

    def test_looks_like_and_extract(self):
        text = "〔raw:mira#t=t_a+3〕\n在讀的小說：讀到第六卷。"
        self.assertTrue(ptr_mod.looks_like(text))
        self.assertEqual(ptr_mod.extract(text), ["raw:mira#t=t_a+3"])
        self.assertFalse(ptr_mod.looks_like("在讀的小說：讀到第六卷。"))


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw = pathlib.Path(self.tmp.name)
        folder = self.raw / "mira"
        folder.mkdir(parents=True)
        rows = [
            {"at": 100.0, "turn": "t_a", "text": "第一句", "room": ["owner", "mira"]},
            {"at": 110.0, "turn": "t_b", "text": "第二句", "room": ["owner", "mira"]},
            {"at": 120.0, "turn": "t_c", "text": "第三句", "room": ["owner", "mira"]},
        ]
        (folder / "2026-10.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8")

    def test_single(self):
        found = ptr_mod.resolve("raw:mira#t=t_b", self.raw)
        self.assertTrue(found["ok"], found)
        self.assertEqual([row["text"] for row in found["rows"]], ["第二句"])

    def test_window_takes_following_turns(self):
        found = ptr_mod.resolve("raw:mira#t=t_a+3", self.raw)
        self.assertTrue(found["ok"], found)
        self.assertEqual([row["text"] for row in found["rows"]],
                         ["第一句", "第二句", "第三句"])
        self.assertEqual(found["missing"], 0)

    def test_missing_tail_is_reported_not_padded(self):
        found = ptr_mod.resolve("raw:mira#t=t_b+5", self.raw)
        self.assertTrue(found["ok"], found)
        self.assertEqual(len(found["rows"]), 2)
        self.assertEqual(found["missing"], 3)

    def test_not_found_does_not_take_neighbours(self):
        """★ 錯的門牌比沒有更壞：找不到就回 not_found，不猜「大概是這句」。"""
        found = ptr_mod.resolve("raw:mira#t=t_zzz+2", self.raw)
        self.assertFalse(found["ok"])
        self.assertEqual(found["reason"], "turn_not_found")
        self.assertEqual(found["rows"], [])

    def test_bad_pointer(self):
        found = ptr_mod.resolve("garbage", self.raw)
        self.assertFalse(found["ok"])
        self.assertEqual(found["reason"], "bad_pointer")

    def test_unknown_entity_is_not_found(self):
        found = ptr_mod.resolve("raw:nori#t=t_a", self.raw)
        self.assertFalse(found["ok"])
        self.assertEqual(found["reason"], "turn_not_found")


class TestResolveThreads(unittest.TestCase):
    """★ 2026-10-26 實測的坑：原文按寫入時間混寫所有線，一輪至少兩行。
    以前 `+n` 數「行」⇒ 私聊窗口打開來混進群聊的行，自己的最後一輪反而被擠掉。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw = pathlib.Path(self.tmp.name)
        dm, group = ["mira", "owner"], ["nori", "mira", "owner"]

        def row(turn_id, thread, role, text, room):
            return {"turn": turn_id, "thread": thread, "role": role, "text": text, "room": room}

        rows = [row("t_a", "dm", "user", "A?", dm), row("t_a", "dm", "assistant", "A!", dm),
                row("g_1", "grp", "user", "群聊插話", group),
                row("g_1", "grp", "assistant", "群聊回覆", group),
                row("t_b", "dm", "user", "B?", dm), row("t_b", "dm", "assistant", "B!", dm),
                row("t_c", "dm", "user", "C?", dm), row("t_c", "dm", "assistant", "C!", dm)]
        folder = self.raw / "mira"
        folder.mkdir(parents=True)
        (folder / "2026-10.jsonl").write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in rows) + "\n",
            encoding="utf-8")

    def test_window_stays_on_its_thread_and_counts_turns(self):
        pointer = ptr_mod.make("mira", ["t_a", "t_a", "t_b", "t_b"])   # 提煉器的傳法
        found = ptr_mod.resolve(pointer, self.raw)
        self.assertTrue(found["ok"], found)
        self.assertEqual([row["text"] for row in found["rows"]], ["A?", "A!", "B?", "B!"])
        self.assertEqual((found["turns"], found["missing"], found["thread"]), (2, 0, "dm"))

    def test_anchor_in_other_thread_reads_only_that_thread(self):
        found = ptr_mod.resolve("raw:mira#t=g_1+2", self.raw)
        self.assertEqual([row["text"] for row in found["rows"]], ["群聊插話", "群聊回覆"])
        self.assertEqual(found["missing"], 1, "群聊這條線只剩一輪")

    def test_max_turns_stops_and_points_at_the_next_turn(self):
        found = ptr_mod.resolve("raw:mira#t=t_a+3", self.raw, max_turns=2)
        self.assertEqual([row["text"] for row in found["rows"]], ["A?", "A!", "B?", "B!"])
        self.assertTrue(found["truncated"])
        self.assertEqual((found["next_turn"], found["missing"]), ("t_c", 0))
        whole = ptr_mod.resolve("raw:mira#t=t_a+3", self.raw, max_turns=3)
        self.assertFalse(whole["truncated"])
        self.assertEqual(whole["next_turn"], "")


class TestWindowStorePointer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "facts.db"

    def test_upsert_writes_pointer_and_turn_ids(self):
        store = WindowStore(self.db)
        store.upsert({"window_id": "w1", "entity": "mira", "thread": "th-1",
                      "started_at": 100.0, "ended_at": 200.0, "turns": 3,
                      "topic": "在讀的小說", "summary": "讀到第六卷。",
                      "turn_ids": ["t_a", "t_b", "t_c"], "ptr": "raw:mira#t=t_a+3"})
        row = store.get("w1")
        self.assertEqual(row["ptr"], "raw:mira#t=t_a+3")
        self.assertEqual(json.loads(row["turn_ids"]), ["t_a", "t_b", "t_c"])
        self.assertEqual(store.stats("mira")["with_ptr"], 1)

    def test_old_table_gets_migrated_in_place(self):
        """舊庫（沒有 ptr 欄位）不能被重建：就地補欄位，舊摘要照讀。"""
        db = sqlite3.connect(self.db)
        db.executescript("""CREATE TABLE window_summaries (
            window_id TEXT PRIMARY KEY, entity TEXT, thread TEXT, started_at REAL,
            ended_at REAL, turns INTEGER, topic TEXT, summary TEXT, people TEXT,
            open_loops TEXT, run_id TEXT, at REAL);""")
        db.execute("INSERT INTO window_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("old", "mira", "th-1", 1.0, 2.0, 4, "舊", "舊摘要", "[]", "[]", "r0", 2.0))
        db.commit()
        db.close()
        store = WindowStore(self.db)
        before = store.stats("mira")
        self.assertEqual((before["total"], before["with_ptr"]), (1, 0))
        store.upsert({"window_id": "new", "entity": "mira", "thread": "th-1",
                      "started_at": 3.0, "ended_at": 4.0, "turns": 2, "topic": "新",
                      "summary": "新摘要", "turn_ids": ["t_x"], "ptr": "raw:mira#t=t_x"})
        after = store.stats("mira")
        self.assertEqual((after["total"], after["with_ptr"]), (2, 1))
        self.assertEqual(store.get("old")["summary"], "舊摘要")


class FakeThread:
    def __init__(self, entity, thread_id, participants, turns):
        self.key = ThreadKey(scope="owner", entity=entity, scene="chat", purpose="talk")
        self.id = thread_id
        self.participants = set(participants)
        self.segment = list(turns)


def turn(turn_id, text, *, at, role="user", speaker="owner"):
    return TurnRecord(turn_id=turn_id, role=role, text=text, source="app", at=at,
                      speaker=speaker)


class OpenCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.memory = SqliteMemory(self.root / "facts.db", seed=False)
        self.raw = RawLog(self.root / "raw")
        now = time.time() - 3600
        self.raw.sync_thread(FakeThread("mira", "th-1", {"owner"}, [
            turn("t_a", "主人在读佐々木とピーちゃん，读到第六卷。", at=now),
            turn("t_b", "へえ、小説？私も読みたい。", at=now + 20, role="assistant",
                 speaker="mira"),
        ]))
        self.raw.sync_thread(FakeThread("mira", "th-2", {"bob"}, [
            turn("t_x", "Bob 说他下个月要搬家，别告诉别人。", at=now + 40, speaker="bob"),
        ]))
        self.search = MemorySearch(memory=self.memory, raw_log=self.raw,
                                   db_path=self.root / "facts.db")

    def test_search_results_carry_pointer(self):
        out = self.search.search(entity="mira", present={"mira", "owner"},
                                 query="小説", limit=5, layers=("turn",))
        self.assertTrue(out["turns"], out)
        self.assertTrue(all(item["ptr"].startswith("raw:mira#t=") for item in out["turns"]),
                        out["turns"])

    def test_open_returns_verbatim_text(self):
        out = self.search.search(entity="mira", present={"mira", "owner"},
                                 query="第六卷", limit=5, layers=("turn",))
        pointer = out["turns"][0]["ptr"]
        opened = self.search.open(entity="mira", present={"mira", "owner"},
                                  pointer=pointer)
        self.assertTrue(opened["ok"], opened)
        self.assertIn("第六卷", opened["rows"][0]["text"])

    def test_open_rejects_other_entity_pointer(self):
        opened = self.search.open(entity="mira", present={"mira", "owner"},
                                  pointer="raw:nori#t=t_a")
        self.assertFalse(opened["ok"])
        self.assertEqual(opened["error"], "turn_not_found")

    def test_open_respects_disclosure(self):
        """門牌指得到，但房間裡有外人 ⇒ 一樣不給（記得不代表說得出口）。"""
        opened = self.search.open(entity="mira", present={"mira", "owner"},
                                  pointer="raw:mira#t=t_x")
        self.assertFalse(opened["ok"], opened)
        self.assertEqual(opened["error"], "turn_not_found")
        # Bob 在場時他自己那段給看
        allowed = self.search.open(entity="mira", present={"mira", "bob"},
                                   pointer="raw:mira#t=t_x")
        self.assertTrue(allowed["ok"], allowed)

    def test_open_reports_missing_tail(self):
        opened = self.search.open(entity="mira", present={"mira", "owner"},
                                  pointer="raw:mira#t=t_a+9")
        self.assertTrue(opened["ok"], opened)
        self.assertGreater(opened["missing"], 0)

    def test_open_caps_a_huge_count_and_hands_out_the_next_door(self):
        """`+n` 是模型抄進來的：`+999999` 不能把整份原文塞進下一輪請求。"""
        now = time.time() - 600
        self.raw.sync_thread(FakeThread("mira", "th-long", {"owner"}, [
            turn(f"t_{index:02d}", f"第{index}句", at=now + index) for index in range(6)]))
        with mock.patch("agent.brain.runtime.memory.memory_open.OPEN_MAX_TURNS", 2):
            opened = self.search.open(entity="mira", present={"mira", "owner"},
                                      pointer="raw:mira#t=t_00+999999")
            self.assertTrue(opened["ok"], opened)
            self.assertEqual([row["text"] for row in opened["rows"]], ["第0句", "第1句"])
            self.assertTrue(opened["truncated"])
            self.assertEqual(opened["more"], "raw:mira#t=t_02+999997")
            following = self.search.open(entity="mira", present={"mira", "owner"},
                                         pointer=opened["more"])
            self.assertEqual([row["text"] for row in following["rows"]], ["第2句", "第3句"])
        with mock.patch("agent.brain.runtime.memory.memory_open.OPEN_MAX_ROWS", 3):
            opened = self.search.open(entity="mira", present={"mira", "owner"},
                                      pointer="raw:mira#t=t_00+6")
            self.assertEqual(len(opened["rows"]), 3)
            self.assertTrue(opened["truncated"])
            self.assertEqual(opened["more"], "raw:mira#t=t_03+3")

    def _write_pairs(self, thread, count):
        """一輪兩行（她聽到的＋她說的），直接寫進原文月檔（實際的原文形狀）。"""
        now = time.time() - 300
        path = self.raw.month_path("mira", now)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for index in range(count):
                for role, text in (("user", f"問{index}"), ("assistant", f"答{index}")):
                    handle.write(json.dumps({"at": now + index, "turn": f"p_{index:02d}",
                                             "thread": thread, "role": role, "text": text,
                                             "room": ["mira", "owner"]},
                                            ensure_ascii=False) + "\n")
        return [text for index in range(count) for text in (f"問{index}", f"答{index}")]

    def _page_through(self, pointer):
        texts, pages = [], 0
        while pointer:
            opened = self.search.open(entity="mira", present={"mira", "owner"},
                                      pointer=pointer)
            self.assertTrue(opened["ok"], opened)
            texts += [row["text"] for row in opened["rows"]]
            self.assertEqual(opened["truncated"], "more" in opened)
            pointer = opened.get("more", "")
            pages += 1
            self.assertLess(pages, 50, "翻頁停不下來")
        return texts, pages

    def test_row_cap_cuts_at_turn_boundary_and_hands_out_more(self):
        """★ review.md 2026-10-26 第 3 條：被行數上限切掉時以前只有 truncated、沒有 more。"""
        self._write_pairs("th-pairs", 4)
        with mock.patch("agent.brain.runtime.memory.memory_open.OPEN_MAX_ROWS", 3):
            opened = self.search.open(entity="mira", present={"mira", "owner"},
                                      pointer="raw:mira#t=p_00+4")
        self.assertEqual([row["text"] for row in opened["rows"]], ["問0", "答0"],
                         "不把一輪切成兩半")
        self.assertTrue(opened["truncated"])
        self.assertEqual(opened["more"], "raw:mira#t=p_01+3")

    def test_paging_with_either_cap_is_complete_and_never_repeats(self):
        expected = self._write_pairs("th-pairs", 7)
        for turns_cap, rows_cap in ((3, 60), (20, 5), (2, 3)):
            with mock.patch("agent.brain.runtime.memory.memory_open.OPEN_MAX_TURNS", turns_cap), \
                    mock.patch("agent.brain.runtime.memory.memory_open.OPEN_MAX_ROWS", rows_cap):
                texts, pages = self._page_through("raw:mira#t=p_00+7")
            self.assertEqual(texts, expected, (turns_cap, rows_cap))
            self.assertGreater(pages, 1, (turns_cap, rows_cap))

    def test_tool_wraps_open(self):
        tool = MemoryOpenTool(self.search)
        ctx = ToolContext(entity="mira", principal="owner", thread_id="th-1",
                          source="app", voice=None)
        result = tool.run(ctx, {"ptr": "raw:mira#t=t_a"})
        self.assertTrue(result.ok, result.error)
        self.assertIn("第六卷", result.data["rows"][0]["text"])
        bad = tool.run(ctx, {"ptr": ""})
        self.assertFalse(bad.ok)
        self.assertEqual(bad.error, "empty_pointer")


class TestWindowBackfill(unittest.TestCase):
    def test_corrects_pointers_written_with_row_counts(self):
        """舊算法把一輪兩行算成兩輪（`+4`）；門牌完全由 turn_ids 決定 ⇒ 重算後改正。"""
        import contextlib
        import io
        import sys as _sys
        _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "agent" / "tools"))
        import backfill_window_ptrs
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            folder = root / "memory" / "windows" / "mira"
            folder.mkdir(parents=True)
            base = {"entity": "mira", "thread": "dm", "started_at": 1.0, "ended_at": 2.0,
                    "turns": 4, "topic": "t", "summary": "s", "turn_ids": ["t_a", "t_a", "t_b", "t_b"]}
            records = {"stale": {**base, "window_id": "stale", "ptr": "raw:mira#t=t_a+4"},
                       "good": {**base, "window_id": "good", "ptr": "raw:mira#t=t_a+2"},
                       "bare": {**base, "window_id": "bare"}}
            for name, record in records.items():
                (folder / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                backfill_window_ptrs.main(["--data", str(root), "--no-verify"])
            self.assertIn("filled=1 corrected=1 already=1", out.getvalue())
            for name in records:
                body = json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))
                self.assertEqual(body["ptr"], "raw:mira#t=t_a+2", name)


class TestCarryBackfill(unittest.TestCase):
    """承接段回填：只認**逐字相等**，找不到或多條就跳過（不猜）。"""

    def _ep(self, thread, entity, turn_ids, summary):
        return {"id": f"ep_{thread}", "thread": thread, "entity": entity,
                "summary": summary,
                "turns": [{"turn": item, "role": "user", "text": "x"} for item in turn_ids]}

    @staticmethod
    def _thread(thread_id, entity, carry):
        """`threads.json` 的實際形狀：角色在 `key.entity`（`Thread.to_store_dict`）。"""
        return {"id": thread_id, "key": {"entity": entity}, "carry_summary": carry}

    def test_fills_exact_match(self):
        from agent.brain.runtime.carry_backfill import plan
        episodes = [self._ep("th-1", "mira", ["t_a", "t_b"], "在讀的小說：讀到第六卷。")]
        threads = [{"id": "th-1", "carry_summary": "在讀的小說：讀到第六卷。"}]
        changes, stats = plan(threads, episodes)
        self.assertEqual(stats["filled"], 1, stats)
        self.assertEqual(changes[0]["ptr"], "raw:mira#t=t_a+2")
        self.assertTrue(changes[0]["carry"].startswith("〔raw:mira#t=t_a+2〕"))

    def test_already_has_pointer_is_left_alone(self):
        from agent.brain.runtime.carry_backfill import plan
        episodes = [self._ep("th-1", "mira", ["t_a"], "在讀的小說。")]
        threads = [{"id": "th-1", "carry_summary": "〔raw:mira#t=t_a〕\n在讀的小說。"}]
        changes, stats = plan(threads, episodes)
        self.assertEqual((len(changes), stats["already"]), (0, 1))

    def test_no_match_is_skipped(self):
        from agent.brain.runtime.carry_backfill import plan
        episodes = [self._ep("th-1", "mira", ["t_a"], "完全不同的一段。")]
        threads = [self._thread("th-1", "mira", "在讀的小說：讀到第六卷。")]
        changes, stats = plan(threads, episodes)
        self.assertEqual((len(changes), stats["skipped_no_match"]), (0, 1))

    def test_ambiguous_match_is_skipped(self):
        from agent.brain.runtime.carry_backfill import plan
        episodes = [self._ep("th-9", "mira", ["t_a"], "同一段話。"),
                    self._ep("th-8", "mira", ["t_b"], "同一段話。")]
        threads = [self._thread("th-1", "mira", "同一段話。")]
        changes, stats = plan(threads, episodes)
        self.assertEqual((len(changes), stats["skipped_ambiguous"]), (0, 1))

    def test_same_thread_identical_summaries_takes_latest(self):
        """同一條線封過多段、摘要文字相同 ⇒ 取最近那次（carry 就是最後一次封片的產物）。"""
        from agent.brain.runtime.carry_backfill import plan
        old = self._ep("th-1", "mira", ["t_old"], "同一段話。")
        old["ended_at"] = 100.0
        new = self._ep("th-1", "mira", ["t_new"], "同一段話。")
        new["ended_at"] = 900.0
        threads = [{"id": "th-1", "carry_summary": "同一段話。"}]
        changes, stats = plan(threads, [new, old])
        self.assertEqual(stats["filled"], 1, stats)
        self.assertEqual(changes[0]["ptr"], "raw:mira#t=t_new")

    def test_episode_without_turn_ids_is_skipped(self):
        from agent.brain.runtime.carry_backfill import plan
        episodes = [self._ep("th-1", "mira", [None, None], "某段。")]
        threads = [{"id": "th-1", "carry_summary": "某段。"}]
        changes, stats = plan(threads, episodes)
        self.assertEqual((len(changes), stats["skipped_no_turns"]), (0, 1))

    def test_cross_thread_match_requires_same_entity(self):
        """★ 唯一同文本的 episode 是別的角色的 ⇒ 不准拿來回填（那是別人倉裡的門牌）。"""
        from agent.brain.runtime.carry_backfill import plan
        episodes = [self._ep("th-k", "nori", ["t_k"], "一起看了煙火。")]
        changes, stats = plan([self._thread("th-m", "mira", "一起看了煙火。")], episodes)
        self.assertEqual((len(changes), stats["skipped_no_match"]), (0, 1), stats)
        # 同一個角色的跨線比對照常可用
        changes, stats = plan([self._thread("th-k2", "nori", "一起看了煙火。")], episodes)
        self.assertEqual([item["ptr"] for item in changes], ["raw:nori#t=t_k"])

    def test_cross_thread_without_entity_is_skipped(self):
        from agent.brain.runtime.carry_backfill import plan
        episodes = [self._ep("th-k", "nori", ["t_k"], "一起看了煙火。")]
        changes, stats = plan([{"id": "th-x", "carry_summary": "一起看了煙火。"}], episodes)
        self.assertEqual((len(changes), stats["skipped_no_entity"]), (0, 1), stats)

    def test_row_counted_pointer_is_corrected(self):
        """★ 舊算法按原文行數算（一輪兩行 ⇒ `+4`）：已帶門牌也要重算、換掉。"""
        from agent.brain.runtime.carry_backfill import plan
        episodes = [self._ep("th-1", "mira", ["t_a", "t_a", "t_b", "t_b"], "在讀的小說。")]
        threads = [self._thread("th-1", "mira", "〔raw:mira#t=t_a+4〕\n在讀的小說。")]
        changes, stats = plan(threads, episodes)
        self.assertEqual((stats["corrected"], stats["already"]), (1, 0), stats)
        self.assertEqual(changes[0]["carry"], "〔raw:mira#t=t_a+2〕\n在讀的小說。")

    def test_prefixed_carry_without_episode_is_unverified(self):
        from agent.brain.runtime.carry_backfill import plan
        threads = [self._thread("th-1", "mira", "〔raw:mira#t=t_a+4〕\n對不到的一段。")]
        changes, stats = plan(threads, [])
        self.assertEqual((len(changes), stats["unverified"], stats["already"]), (0, 1, 0))

    def test_apply_writes_backup_and_prefix(self):
        import json as _json
        from agent.brain.runtime.carry_backfill import apply
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "threads.json").write_text(_json.dumps(
                {"threads": [{"id": "th-1", "carry_summary": "某段話。"}], "current": {}, "gen": 1},
                ensure_ascii=False), encoding="utf-8")
            (root / "episodes.jsonl").write_text(_json.dumps(
                self._ep("th-1", "mira", ["t_a"], "某段話。"), ensure_ascii=False) + "\n",
                encoding="utf-8")
            stats = apply(root, dry_run=False)
            self.assertEqual(stats["filled"], 1, stats)
            body = _json.loads((root / "threads.json").read_text(encoding="utf-8"))
            self.assertTrue(body["threads"][0]["carry_summary"].startswith("〔raw:mira#t=t_a〕"))
            self.assertTrue(pathlib.Path(stats["backup"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
