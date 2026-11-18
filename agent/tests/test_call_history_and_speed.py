#!/usr/bin/env python3
"""通話兩件事（owner 2026-10-29）：

① **通話時只跑本地快子代理**（不跑 LLM 子代理）——實測 `subagents_ms=3768` 全卡在她
   開口之前，通話要的是快。
② **通話內容不進 App 的對話紀錄**——只留一條 `kind="call-log"` 的標記
   （「發生過通話 3分12秒」）。通話的每一輪標 `kind="call"`，`/api/turns` 濾掉。

判據：
  · `SubAgentOrchestrator.think(fast_only=True)` ⇒ 只跑 `agent.fast`；
  · `phone:call` 的回合 ⇒ outbox 那列 `kind="call"`；
  · `/api/turns?source=phone,api` ⇒ 回不到 `kind="call"`，但看得到 `kind="call-log"`；
  · 掛斷 ⇒ 寫**一條**標記，文字含時長；太短（<`CALL_LOG_MIN_S`）不寫；
    Discord 語音房掛斷**不寫**（它本來就不在 App 的紀錄裡）。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose import voice_api
from agent.brain.domain.auth import AuthContext, Role
from agent.brain.runtime.delivery.outbox import TurnOutbox, new_turn_id
from agent.brain.runtime.subagents import Note, SubAgentOrchestrator
from agent.server import api as api_module

OWNER = AuthContext(principal="owner", role=Role.OWNER, capabilities=frozenset({"chat"}))


class _Agent:
    """最小子代理替身：記下被叫幾次。"""

    def __init__(self, name, *, fast):
        self.name = name
        self.fast = fast
        self.calls = 0

    def available(self):
        return True

    def run(self, *, entity, present, text, meta):
        self.calls += 1
        return [f"{self.name}:{text[:8]}"]


class TestFastOnlyInCall(unittest.TestCase):
    """① 通話只跑 local 子代理。"""

    def test_fast_only_skips_llm_subagents(self):
        fast = _Agent("recall", fast=True)
        slow = _Agent("inner", fast=False)
        orchestrator = SubAgentOrchestrator(agents=[fast, slow])
        notes = orchestrator.think(entity="mira", present={"owner"},
                                   text="今日は何してた", fast_only=True)
        self.assertEqual(fast.calls, 1, "快的（本地檢索）應該跑")
        self.assertEqual(slow.calls, 0, "LLM 子代理在通話裡不該跑")
        self.assertTrue(notes)
        self.assertTrue(all(note.agent == "recall" for note in notes))

    def test_without_fast_only_both_run(self):
        fast = _Agent("recall", fast=True)
        slow = _Agent("inner", fast=False)
        orchestrator = SubAgentOrchestrator(agents=[fast, slow])
        orchestrator.think(entity="mira", present={"owner"},
                           text="今日は何してた", fast_only=False)
        self.assertEqual(fast.calls, 1)
        self.assertEqual(slow.calls, 1, "一般回合兩個都跑（行為不變）")

    def test_fast_only_before_truncation(self):
        """`max_agents=1` 且慢的排在前面 ⇒ 仍要選到快的。"""
        slow = _Agent("inner", fast=False)
        fast = _Agent("recall", fast=True)
        orchestrator = SubAgentOrchestrator(agents=[slow, fast], max_agents=1)
        orchestrator.think(entity="mira", present={"owner"},
                           text="今日は何してた", fast_only=True)
        self.assertEqual(fast.calls, 1)
        self.assertEqual(slow.calls, 0)


class TestCallNotInAppHistory(unittest.TestCase):
    """② 通話內容不進 App 的對話紀錄。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.outbox = TurnOutbox(root=self.root)

    def _record(self, *, source, text, kind="", asked=""):
        return self.outbox.record(
            turn_id=new_turn_id(), principal="owner", entity="mira",
            text=text, asked=asked, source=source, kind=kind)

    class _App:
        def __init__(self, box):
            self.outbox = box

    class _State:
        def __init__(self, app):
            self.app = app

    def _turns(self):
        state = self._State(self._App(self.outbox))
        code, data = api_module.list_turns(state, {"source": ["phone,api"]}, auth=OWNER)
        self.assertEqual(code, 200, data)
        return data["turns"]

    def test_call_content_is_hidden_but_marker_shows(self):
        self._record(source="phone:call", text="通話の中身", asked="今日は何してた",
                     kind="call")
        self._record(source="phone:call", text="（通話 3分12秒）", kind="call-log")
        rows = self._turns()
        texts = [str(row.get("text") or "") for row in rows]
        self.assertNotIn("通話の中身", texts, "通話內容不該出現在 App 的紀錄裡")
        self.assertIn("（通話 3分12秒）", texts, "「發生過通話」的標記要看得到")

    def test_ordinary_turn_still_shows(self):
        self._record(source="phone:owner", text="ふつうの返事", asked="おはよう")
        rows = self._turns()
        self.assertEqual([str(row.get("text") or "") for row in rows], ["ふつうの返事"])

    def test_old_rows_without_kind_still_show(self):
        """舊列沒有 `kind` 鍵 ⇒ 照舊當一般回合。"""
        with (self.root / "index.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"turn_id": "t-old-1", "at": time.time(),
                                     "principal": "owner", "entity": "mira",
                                     "text": "古い返事", "source": "phone:owner"},
                                    ensure_ascii=False) + "\n")
        rows = self._turns()
        self.assertEqual([str(row.get("text") or "") for row in rows], ["古い返事"])


class TestCallLogOnHangup(unittest.TestCase):
    """② 掛斷寫一條標記。"""

    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.outbox = self.state.app.outbox

    def _rows(self):
        path = self.outbox.index_path
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def _call(self):
        return api_module.dispatch_post(self.state, "/api/voice/call",
                                        {"entity": "mira"}, auth=OWNER)[1]["session"]

    def test_hangup_writes_one_marker_with_duration(self):
        session = self._call()
        # 把開始時間往前撥 ⇒ 時長 3 分 12 秒
        self.state.app.voice._sessions["mira"].created_at = time.time() - 192
        before = len(self._rows())
        data = api_module.dispatch_post(self.state, "/api/voice/hangup",
                                        {"entity": "mira", "call_id": session["id"]},
                                        auth=OWNER)[1]
        self.assertTrue(data["ok"], data)
        rows = self._rows()
        self.assertEqual(len(rows), before + 1, "掛斷要寫且只寫一條")
        row = rows[-1]
        self.assertEqual(row.get("kind"), "call-log")
        self.assertIn("3分12秒", str(row.get("text") or ""))
        self.assertEqual(row.get("asked"), "", "標記行不帶使用者的話")
        # 客戶端拿秒數按界面語言自己說（三種語言）⇒ 伺服器要給數字
        self.assertEqual(row.get("call_s"), 192)

    def test_short_call_leaves_no_marker(self):
        session = self._call()
        before = len(self._rows())
        api_module.dispatch_post(self.state, "/api/voice/hangup",
                                 {"entity": "mira", "call_id": session["id"]},
                                 auth=OWNER)
        self.assertEqual(len(self._rows()), before, "太短的通話不留痕")

    def test_discord_voice_hangup_leaves_no_marker(self):
        """Discord 語音房掛斷不寫（它本來就不在 App 的紀錄裡）。"""
        self.state.app.voice.call("mira", end="discord", principal="owner")
        self.state.app.voice._sessions["mira"].created_at = time.time() - 300
        before = len(self._rows())
        api_module.dispatch_post(self.state, "/api/voice/hangup",
                                 {"entity": "mira"}, auth=OWNER)
        self.assertEqual(len(self._rows()), before)

    def test_duration_text(self):
        self.assertEqual(voice_api._duration_text(42), "42秒")
        self.assertEqual(voice_api._duration_text(192), "3分12秒")
        self.assertEqual(voice_api._duration_text(60), "1分0秒")
        self.assertEqual(voice_api._duration_text(0), "0秒")


class TestCallTurnEndToEnd(unittest.TestCase):
    """端到端：真的跑一輪 `phone:call` ⇒ outbox 那列 `kind="call"`，且 App 看不到。"""

    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.outbox = self.state.app.outbox

    def _rows(self):
        path = self.outbox.index_path
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_call_turn_is_kind_call_and_hidden_from_app(self):
        api_module.dispatch_post(self.state, "/api/voice/call",
                                 {"entity": "mira"}, auth=OWNER)
        list(self.state.send_stream(text="今日は何してた", entity="mira",
                                   principal="owner", mode="work",
                                   source_override="phone:call"))
        rows = self._rows()
        self.assertTrue(rows, "這一輪應該落進 outbox")
        call_rows = [row for row in rows if str(row.get("source") or "") == "phone:call"]
        self.assertTrue(call_rows, rows)
        for row in call_rows:
            self.assertEqual(row.get("kind"), "call", "通話輪要標 kind=call")

        # App 的對話紀錄看不到它
        code, data = api_module.list_turns(self.state, {"source": ["phone,api"]},
                                           auth=OWNER)
        self.assertEqual(code, 200, data)
        self.assertFalse([row for row in data["turns"]
                          if str(row.get("source") or "") == "phone:call"],
                         "通話內容不該出現在 App 的對話紀錄裡")

    def test_ordinary_turn_still_has_no_call_kind(self):
        list(self.state.send_stream(text="おはよう", entity="mira",
                                   principal="owner", mode="chat"))
        rows = [row for row in self._rows()
                if str(row.get("source") or "").startswith("phone:")]
        self.assertTrue(rows, rows)
        for row in rows:
            self.assertNotEqual(row.get("kind"), "call", "一般回合不該被標成通話")


class TestBackfillCallHistory(unittest.TestCase):
    """回填：已存在的通話回合從 App 紀錄裡拿掉，每通留一條標記。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = pathlib.Path(self._tmp.name)
        self.index = self.data / "outbox" / "index.jsonl"
        self.index.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, rows):
        self.index.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8")

    def _read(self):
        return [json.loads(line) for line in
                self.index.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _call_row(self, turn_id, at, asked="もしもし"):
        return {"turn_id": turn_id, "at": at, "principal": "owner",
                "entity": "mira", "thread": "", "text": f"返事{turn_id}",
                "asked": asked, "ok": True, "error": "", "frames": 1,
                "speech": 1, "audio": False, "bytes": 0, "source": "phone:call"}

    def test_marks_call_rows_and_inserts_one_marker(self):
        from agent.tools.backfill_call_history import apply as backfill
        self._write([self._call_row("c1", 100.0), self._call_row("c2", 130.0)])
        stats = backfill(self.data, dry_run=False)
        self.assertEqual(stats["marked"], 2)
        self.assertEqual(stats["markers"], 1)
        rows = self._read()
        self.assertEqual([row.get("kind") for row in rows], ["call", "call", "call-log"])
        marker = rows[-1]
        self.assertEqual(marker.get("text"), "（通話）")
        self.assertEqual(marker.get("asked"), "")
        self.assertGreaterEqual(float(marker["at"]), 130.0, "標記要在那一通之後")

    def test_two_sessions_get_two_markers(self):
        from agent.tools.backfill_call_history import apply as backfill
        self._write([self._call_row("c1", 100.0), self._call_row("c2", 1000.0)])
        stats = backfill(self.data, dry_run=False)
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["markers"], 2)

    def test_dry_run_does_not_write(self):
        from agent.tools.backfill_call_history import apply as backfill
        self._write([self._call_row("c1", 100.0)])
        before = self.index.read_text(encoding="utf-8")
        backfill(self.data, dry_run=True)
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)

    def test_idempotent(self):
        from agent.tools.backfill_call_history import apply as backfill
        self._write([self._call_row("c1", 100.0)])
        backfill(self.data, dry_run=False)
        first = self._read()
        stats = backfill(self.data, dry_run=False)
        self.assertEqual(stats["call_rows"], 0, "第二次不該再動")
        self.assertEqual(len(self._read()), len(first))

    def test_backup_is_written(self):
        from agent.tools.backfill_call_history import apply as backfill
        self._write([self._call_row("c1", 100.0)])
        backfill(self.data, dry_run=False)
        backups = list(self.index.parent.glob("index.jsonl.bak-call-*"))
        self.assertEqual(len(backups), 1)

    def test_ordinary_rows_untouched(self):
        from agent.tools.backfill_call_history import apply as backfill
        self._write([{"turn_id": "n1", "at": 100.0, "principal": "owner",
                      "entity": "mira", "text": "ふつう", "asked": "おはよう",
                      "source": "phone:owner"}])
        backfill(self.data, dry_run=False)
        rows = self._read()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("kind", rows[0])


if __name__ == "__main__":
    unittest.main()
