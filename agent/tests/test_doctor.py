#!/usr/bin/env python3
"""體檢器（哨兵）：判據借自 ECHO，規矩寫死在代碼裡。

判據：
  ① **每層至少一項檢查**；某層 0 項 ⇒ `error`（結構洞，不是「沒事」）；
  ② 讀不到／沒資料 ⇒ `pending`，**不准**顯示成 `ok`（看不到 ≠ 沒有）；
  ③ 檢查自己拋錯 ⇒ `pending` ＋ 例外型別（哨兵壞掉也要看得見）；
  ④ 門牌覆蓋率與「抽樣回原文」是索引層與檢索層的硬判據（只報數量不算體檢）。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime import doctor
from agent.brain.runtime.memory.window_store import WindowStore

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _by_name(report, check: str) -> dict:
    return next(item for item in report["items"] if item["check"] == check)


class DataCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = pathlib.Path(self.tmp.name)

    # ── 造資料的兩個小工具 ──────────────────────────────────────────────────
    def _raw(self, entity: str = "mira", rows=None):
        folder = self.data / "raw" / entity
        folder.mkdir(parents=True, exist_ok=True)
        now = time.time() - 3600
        rows = rows or [{"at": now, "turn": "t_a", "text": "第一句", "room": ["owner", entity]},
                        {"at": now + 10, "turn": "t_b", "text": "第二句", "room": ["owner", entity]}]
        (folder / "2026-10.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8")

    def _window(self, window_id: str = "w1", *, ptr: str = "", turn_ids=None,
                entity: str = "mira"):
        (self.data / "memory").mkdir(parents=True, exist_ok=True)
        WindowStore(self.data / "memory" / "facts.db").upsert({
            "window_id": window_id, "entity": entity, "thread": "th-1",
            "started_at": 100.0, "ended_at": 200.0, "turns": 2, "topic": "在讀的小說",
            "summary": "讀到第六卷。", "turn_ids": turn_ids or ["t_a", "t_b"], "ptr": ptr})


class TestStructure(DataCase):
    def test_every_layer_has_at_least_one_check(self):
        report = doctor.run(self.data, "mira")
        for layer in doctor.LAYERS:
            item = next(row for row in report["items"]
                        if row["check"] == "has-checks" and row.get("subject") == layer)
            self.assertEqual(item["status"], "ok", f"{layer}: {item['detail']}")

    def test_layer_without_checks_is_an_error(self):
        """★ 判據①的本體：把一層的檢查全拿掉 ⇒ 必須報 error，而不是「全綠」。"""
        saved = list(doctor._CHECKS)
        try:
            doctor._CHECKS[:] = [item for item in doctor._CHECKS
                                 if item["layer"] != "L5-retrieval"]
            report = doctor.run(self.data, "mira")
            item = next(row for row in report["items"]
                        if row["check"] == "has-checks" and row.get("subject") == "L5-retrieval")
            self.assertEqual(item["status"], "error", item)
            self.assertIn("沒有任何檢查項", item["detail"])
            self.assertFalse(report["ok"])
        finally:
            doctor._CHECKS[:] = saved

    def test_evidence_must_be_declared(self):
        saved = list(doctor._CHECKS)
        try:
            doctor._CHECKS.append({"layer": "L1-self", "name": "no-evidence",
                                   "evidence": "", "run": lambda ctx: ("ok", "x")})
            report = doctor.run(self.data, "mira")
            item = _by_name(report, "evidence-declared")
            self.assertEqual(item["status"], "error")
            self.assertIn("no-evidence", item["detail"])
        finally:
            doctor._CHECKS[:] = saved

    def test_check_that_raises_is_pending_not_ok(self):
        """★ 判據③：哨兵自己壞掉要看得見——拋錯記 pending，不記 ok、也不讓整輪掛掉。"""
        def boom(ctx):
            raise RuntimeError("db gone")

        saved = list(doctor._CHECKS)
        try:
            doctor._CHECKS.append({"layer": "L4-index", "name": "boom",
                                   "evidence": "測試用", "run": boom})
            report = doctor.run(self.data, "mira")
            item = _by_name(report, "boom")
            self.assertEqual(item["status"], "pending")
            self.assertIn("檢查自己拋錯", item["detail"])
        finally:
            doctor._CHECKS[:] = saved


class TestNoFalseGreen(DataCase):
    def test_empty_data_dir_is_never_all_ok(self):
        report = doctor.run(self.data, "mira")
        self.assertNotEqual(report["worst"], "ok")
        self.assertGreaterEqual(report["counts"]["pending"], 1, report["items"])
        # 空資料時每一項要嘛 pending、要嘛 ok（結構項），但不能有「假 error」
        self.assertEqual(report["counts"]["error"], 0, report["items"])

    def test_reading_denied_is_pending(self):
        folder = self.data / "raw" / "mira"
        folder.mkdir(parents=True)
        (folder / "2026-10.jsonl").write_text('{"at": 1, "turn": "t_a"}\n', encoding="utf-8")
        import os
        os.chmod(folder, 0o000)
        self.addCleanup(os.chmod, folder, 0o755)
        report = doctor.run(self.data, "mira")
        item = _by_name(report, "raw-presence")
        self.assertIn(item["status"], ("pending", "warn"), item)


class TestPointerChecks(DataCase):
    def test_no_pointer_is_an_error(self):
        self._raw()
        self._window("w1", ptr="")
        report = doctor.run(self.data, "mira")
        item = _by_name(report, "window-pointers")
        self.assertEqual(item["status"], "error", item)
        self.assertIn("一條都沒有門牌", item["detail"])

    def test_partial_coverage_is_a_warning(self):
        self._raw()
        self._window("w1", ptr="raw:mira#t=t_a+2")
        self._window("w2", ptr="")
        report = doctor.run(self.data, "mira")
        item = _by_name(report, "window-pointers")
        self.assertEqual(item["status"], "warn", item)

    def test_full_coverage_ok_and_roundtrip_works(self):
        self._raw()
        self._window("w1", ptr="raw:mira#t=t_a+2")
        report = doctor.run(self.data, "mira")
        self.assertEqual(_by_name(report, "window-pointers")["status"], "ok")
        self.assertEqual(_by_name(report, "pointer-roundtrip")["status"], "ok")
        self.assertEqual(_by_name(report, "raw-presence")["status"], "ok")

    def test_broken_pointer_is_caught(self):
        """門牌指得到一個原文裡沒有的 turn ⇒ 檢索層必須報 error（斷鏈）。"""
        self._raw()
        self._window("w1", ptr="raw:mira#t=t_zzz+2")
        report = doctor.run(self.data, "mira")
        item = _by_name(report, "pointer-roundtrip")
        self.assertEqual(item["status"], "error", item)
        self.assertIn("turn_not_found", item["detail"])


class TestCarryPointer(DataCase):
    def _threads(self, carry: str):
        (self.data / "threads.json").write_text(json.dumps(
            {"threads": [{"id": "th-1", "carry_summary": carry}], "current": {}, "gen": 1},
            ensure_ascii=False), encoding="utf-8")

    def test_carry_without_pointer_is_error(self):
        self._threads("在讀的小說：讀到第六卷。")
        item = _by_name(doctor.run(self.data, "mira"), "carry-pointer")
        self.assertEqual(item["status"], "error", item)

    def test_carry_with_pointer_is_ok(self):
        self._threads("〔raw:mira#t=t_a+2〕\n在讀的小說：讀到第六卷。")
        item = _by_name(doctor.run(self.data, "mira"), "carry-pointer")
        self.assertEqual(item["status"], "ok", item)

    def test_no_carry_yet_is_pending(self):
        self._threads("")
        item = _by_name(doctor.run(self.data, "mira"), "carry-pointer")
        self.assertEqual(item["status"], "pending", item)


class TestCli(DataCase):
    def test_cli_runs_and_exits_zero_without_errors(self):
        out = subprocess.run([sys.executable, str(ROOT / "agent" / "tools" / "doctor.py"),
                              "--data", str(self.data), "--entity", "mira"],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("體檢", out.stdout)
        self.assertIn("證據：", out.stdout)

    def test_cli_exits_one_on_error(self):
        self._raw()
        self._window("w1", ptr="")
        out = subprocess.run([sys.executable, str(ROOT / "agent" / "tools" / "doctor.py"),
                              "--data", str(self.data), "--entity", "mira", "--json"],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        report = json.loads(out.stdout)
        self.assertFalse(report["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
