#!/usr/bin/env python3
"""可觀測性：每輪一行 JSONL ＋ `/api/metrics`（p50/p95）。

判據：
  ① 一輪真的寫一行（欄位：thread/entity/engage/ms/frames/speech…）；
  ② 語音失敗會計進 `speech_errors`（TTS 掛掉時看板要看得出來）；
  ③ `/api/metrics` 只有主人能看（guest 403、無 token 401）；
  ④ 紀錄**不含原文**（日誌被看到 ≠ 對話被看到）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.state import AppState
from agent.brain.runtime.ops.telemetry import TurnRecorder


class TestTurnRecorder(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ba-tel-")
        self.app = build(brain_delay=0, data_dir=self.root,
                          step_duration=0.02, life_pause=0.005)
        self.state = AppState(self.app)
        self.addCleanup(self.app.shutdown_lives)

    def rows(self) -> list[dict]:
        path = Path(self.app.telemetry.path)
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_one_row_per_turn(self):
        self.state.send("你好", principal="owner")
        self.state.send("晚安", principal="owner")
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        row = rows[0]
        for key in ("at", "thread", "entity", "principal", "engage", "ms", "frames"):
            self.assertIn(key, row)
        self.assertEqual(row["entity"], "mira")
        self.assertGreaterEqual(row["ms"], 0)

    def test_transcript_text_is_not_logged(self):
        secret = "これは日誌に出してはいけない文章です"
        self.state.send(secret, principal="owner")
        raw = Path(self.app.telemetry.path).read_text()
        self.assertNotIn(secret, raw, "日誌不該記原文")
        self.assertIn('"chars"', raw, "只留長度")

    def test_speech_failures_are_counted(self):
        recorder = self.app.telemetry
        recorder.record(thread="t", entity="mira", principal="owner", engage="reply",
                        ms=100, frames=3, speech=0, speech_errors=2)
        metrics = recorder.metrics()
        self.assertEqual(metrics["speech_errors"], 2)

    def test_percentiles(self):
        # ⚠️ 要給 data_root：不給就是 path 的上上層＝系統暫存目錄，/api/metrics 的 data_bytes 會把整個 TMPDIR
        #    掃一遍（2026-10-31 量到這一支吃掉全量測試 140 秒裡的 43 秒）
        recorder = TurnRecorder(path=Path(self.root) / "t.jsonl", data_root=self.root)
        for value in (100, 200, 300, 400, 500):
            recorder.record(thread="t", entity="mira", ms=value)
        latency = recorder.metrics()["latency_ms"]
        self.assertEqual(latency["p50"], 300)
        self.assertEqual(latency["max"], 500)

    def test_stage_marks_are_logged(self):
        """階段打點：記憶檢索／子代理／LLM 開跑各在第幾毫秒（不發給端末）。"""
        self.state.send("今日は何してた？", principal="owner")
        row = self.rows()[-1]
        self.assertIn("recall_ms", row, "記憶檢索那一段要有打點")
        self.assertIn("brain_start_ms", row, "LLM 開跑那一段要有打點")
        self.assertLessEqual(row["recall_ms"], row["brain_start_ms"] + 5,
                             "記憶檢索不該晚於 LLM 開跑")

    # ── 分段打點（2026-10-24）────────────────────────────────────────────────
    def test_turn_row_carries_stage_timings(self):
        """一輪的紀錄要能說出「LLM 開跑／首字／首句音檔」各在第幾毫秒。"""
        self.state.send("こんばんは", principal="owner")
        row = self.rows()[-1]
        for key in ("first_frame_ms", "brain_ms", "first_token_ms",
                    "first_audio_ms", "speech_frame_ms", "done_ms"):
            self.assertIn(key, row, f"缺打點欄位 {key}")
        self.assertIsNotNone(row["brain_ms"], "mock 腦也該吐 thinking 幀")
        self.assertGreaterEqual(row["first_frame_ms"], 0)
        self.assertLessEqual(row["first_frame_ms"], row["ms"] + 50)
        self.assertIsNotNone(row["done_ms"], "一輪正常結束就該有 done 幀")


class TestDefaultLogDir(unittest.TestCase):
    def test_default_is_repo_data_logs(self):
        """沒設 BLUEAGENT_LOG_DIR ⇒ 倉庫根的 data/logs（2026-11-16 搬進 runtime/ops/ 後層數多一層，曾指到 agent/data/logs）。"""
        import os
        from unittest import mock
        repo = Path(__file__).resolve().parents[2]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BLUEAGENT_LOG_DIR", None)
            recorder = TurnRecorder()
        self.assertEqual(recorder.path, repo / "data" / "logs" / "turns.jsonl")


class TestFrameSinkMarks(unittest.TestCase):
    """打點只記第一次、且沒有那一刻時留 None（不要用 0 假裝量到）。"""

    @staticmethod
    def frame(kind, **payload):
        from agent.brain.domain.event import FrameKind, OutFrame
        return OutFrame(kind=FrameKind(kind), payload=payload, thread_id="t",
                        turn_id="u", epoch=1)

    def test_marks_first_of_each_milestone(self):
        import time as _time
        from agent.brain.compose.frames import FrameSink
        sink = FrameSink()
        started = _time.monotonic()
        sink.deliver(self.frame("thinking"))
        _time.sleep(0.02)
        sink.deliver(self.frame("delta", text="a"))
        sink.deliver(self.frame("delta", text="b"))          # 第二個不覆蓋
        sink.deliver(self.frame("speech", audio=""))         # 沒有音檔 ⇒ 不算 first_audio
        _time.sleep(0.02)
        sink.deliver(self.frame("speech", audio="QUJD"))
        sink.deliver(self.frame("done", text="ok"))
        marks = sink.timing(started)
        self.assertIsNotNone(marks["brain_ms"])
        self.assertLess(marks["brain_ms"], marks["first_token_ms"])
        self.assertLess(marks["first_token_ms"], marks["first_audio_ms"])
        self.assertIsNotNone(marks["speech_frame_ms"])
        self.assertLess(marks["speech_frame_ms"], marks["first_audio_ms"])
        self.assertIsNotNone(marks["done_ms"])

    def test_missing_milestones_are_none(self):
        import time as _time
        from agent.brain.compose.frames import FrameSink
        sink = FrameSink()
        started = _time.monotonic()
        sink.deliver(self.frame("thinking"))
        marks = sink.timing(started)
        self.assertIsNone(marks["first_token_ms"])
        self.assertIsNone(marks["first_audio_ms"])


class TestMetricsEndpoint(unittest.TestCase):
    def setUp(self):
        self.app = build(brain_delay=0, data_dir=tempfile.mkdtemp(prefix="ba-tel-http-"),
                          step_duration=0.02, life_pause=0.005)
        self.state = AppState(self.app)
        self.addCleanup(self.app.shutdown_lives)
        self.owner = self.app.auth.owner_context()

    def test_owner_sees_metrics(self):
        from agent.server import api
        self.state.send("你好", principal="owner")
        status, body = api.post_metrics(self.state, auth=self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(body["metrics"]["turns"], 1)

    def test_guest_is_denied(self):
        from agent.server import api
        from agent.brain.runtime.identity.auth import AuthContext, Role
        guest = AuthContext("guest-x", Role.GUEST, frozenset({"chat"}))
        status, body = api.post_metrics(self.state, auth=guest)
        self.assertEqual(status, 403)

    def test_anonymous_is_denied(self):
        from agent.server import api
        status, _body = api.post_metrics(self.state, auth=None)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
