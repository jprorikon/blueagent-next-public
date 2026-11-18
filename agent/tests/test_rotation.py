#!/usr/bin/env python3
"""日誌／資料輪轉（owner 2026-10-22 的第二件事）。

判據（每一條都對應交接檔 §6.2 的驗收）：
  ① 6 MB 的 `turns.jsonl` ⇒ 出現 `turns.jsonl.1`，原檔從 0 開始；
  ② 一條 1000 輪的 thread ⇒ `segment` 只留上限，其餘進 `data/archive/threads-<YYYYMM>.jsonl`（順序不變）；
  ③ 載入時檔案過大 ⇒ 先封存再寫回小檔（`threads.json` 不能切半）；
  ④ `/api/metrics` 有 `rotation`／`data_bytes`／`archive` 數字；
  ⑤ 不誤刪：封存檔裡一行不少、`transcript`（prompt 用的滾動窗）不受影響。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.ids import ThreadKey
from agent.brain.domain.event import SourceClass
from agent.brain.domain.thread import TurnRecord
from agent.brain.runtime.ops.rotation import (RotationGuard, append_archive, dir_bytes,
                                             month_key, rotate_if_needed, usage)
from agent.brain.runtime.ops.telemetry import TurnRecorder
from agent.brain.runtime.threadstore import ThreadStore


def big_file(path: pathlib.Path, size: int, line: str = '{"a":1}\n') -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = line * (size // len(line) + 1)
    path.write_text(body, encoding="utf-8")
    return path


class TestRotateIfNeeded(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.path = self.tmp / "turns.jsonl"

    def test_under_threshold_does_nothing(self):
        self.path.write_text("x" * 100, encoding="utf-8")
        self.assertFalse(rotate_if_needed(self.path, max_bytes=1000, keep=3))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "x" * 100)
        self.assertFalse((self.tmp / "turns.jsonl.1").exists())

    def test_six_megabyte_file_rotates_and_restarts_empty(self):
        big_file(self.path, 6 * 1024 * 1024)
        self.assertTrue(rotate_if_needed(self.path, max_bytes=5 * 1024 * 1024, keep=5))
        self.assertTrue((self.tmp / "turns.jsonl.1").exists())
        self.assertEqual(self.path.stat().st_size, 0, "原檔要從 0 開始")

    def test_generations_are_capped_by_keep(self):
        for _ in range(6):
            big_file(self.path, 2000)
            rotate_if_needed(self.path, max_bytes=1000, keep=2)
        names = sorted(item.name for item in self.tmp.glob("turns.jsonl*"))
        self.assertEqual(names, ["turns.jsonl", "turns.jsonl.1", "turns.jsonl.2"])

    def test_max_bytes_zero_disables(self):
        big_file(self.path, 5000)
        self.assertFalse(rotate_if_needed(self.path, max_bytes=0))
        self.assertGreater(self.path.stat().st_size, 4000)

    def test_guard_rotates_after_n_writes(self):
        guard = RotationGuard(self.path, max_bytes=100, keep=2, every=3)
        self.path.write_text("y" * 200, encoding="utf-8")
        self.assertFalse(guard.note_write())
        self.assertFalse(guard.note_write())
        self.assertTrue(guard.note_write(), "第 3 次才去看大小（每 N 次 stat）")
        self.assertEqual(guard.rotations, 1)


class TestThreadArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.path = self.tmp / "threads.json"
        self.store = ThreadStore(path=self.path, max_segment=200,
                                 archive_dir=self.tmp / "archive")

    def _fill(self, turns: int) -> None:
        key = ThreadKey(scope="owner", entity="mira", scene="dm", purpose="chat")
        attach = self.store.attach(key, now=1000.0, window=600.0, lane=SourceClass.INTERACTIVE)
        thread = attach.thread
        for index in range(turns):
            thread.record(TurnRecord(turn_id=f"t{index}", role="user", text=f"第 {index} 句",
                                     source="test", at=1000.0 + index))

    def test_thousand_turns_are_capped_and_archived_in_order(self):
        self._fill(1000)
        self.store.persist()
        thread = self.store.threads()[0]
        self.assertEqual(len(thread.segment), 200, "記憶體只留上限")
        self.assertEqual(len(thread.transcript), 40, "prompt 的滾動窗本來就有上限")
        files = sorted((self.tmp / "archive").glob("threads-*.jsonl"))
        self.assertEqual(len(files), 1, f"封存檔要在 archive/：{files}")
        rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 800, "1000 - 200 = 800 條被搬走")
        self.assertEqual([row["turn"]["turn"] for row in rows[:3]], ["t0", "t1", "t2"])
        self.assertEqual(rows[-1]["turn"]["turn"], "t799", "順序必須是原始順序")
        self.assertEqual(rows[0]["entity"], "mira")
        self.assertEqual(self.store.archived, 800)

    def test_reload_keeps_archive_and_does_not_duplicate(self):
        self._fill(300)
        self.store.persist()
        before = dir_bytes(self.tmp / "archive")
        again = ThreadStore(path=self.path, max_segment=200, archive_dir=self.tmp / "archive")
        again.persist()
        self.assertEqual(dir_bytes(self.tmp / "archive"), before, "重啟不該重複封存")
        self.assertEqual(len(again.threads()[0].segment), 200)

    def test_load_compresses_an_oversized_state_file(self):
        self._fill(600)
        # 假裝上限很小 ⇒ 載入時就該封存並寫回小檔
        self.store.persist()
        size_before = self.path.stat().st_size
        store = ThreadStore(path=self.path, max_segment=50, max_state_bytes=1024,
                            archive_dir=self.tmp / "archive")
        self.assertLess(self.path.stat().st_size, size_before, "載入後檔案要變小")
        self.assertEqual(len(store.threads()[0].segment), 50)
        self.assertGreater(store.archived, 0)
        reread = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(reread.get("threads"), "寫回來的仍是合法 JSON")

    def test_archive_helper_appends_monthly(self):
        rows = [{"turn": {"turn": "t0"}}, {"turn": {"turn": "t1"}}]
        target = append_archive(self.tmp / "archive", "threads", rows, month="202610")
        self.assertEqual(target.name, "threads-202610.jsonl")
        append_archive(self.tmp / "archive", "threads", rows, month="202610")
        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 4, "append 不覆蓋")
        self.assertEqual(month_key(0), month_key(0))


class TestMetricsExposeDataGovernance(unittest.TestCase):
    def test_metrics_have_rotation_and_data_bytes(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        recorder = TurnRecorder(tmp / "logs" / "turns.jsonl", data_root=tmp,
                                max_bytes=1024, keep=2)
        for index in range(70):
            recorder.record(ms=10, entity="mira", engage="reply", pad="x" * 60)
        metrics = recorder.metrics()
        self.assertEqual(metrics["rotation"]["max_bytes"], 1024)
        self.assertGreaterEqual(metrics["rotation"]["rotations"], 1, "70 行就該轉過")
        self.assertTrue((tmp / "logs" / "turns.jsonl.1").exists())
        self.assertIn("data_bytes", metrics)
        self.assertGreater(metrics["data_bytes"]["total"], 0)
        self.assertLess(recorder.path.stat().st_size, 1024)

    def test_usage_reports_each_entry(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        (tmp / "logs").mkdir()
        (tmp / "logs" / "a.jsonl").write_text("x" * 100, encoding="utf-8")
        items = usage(tmp)
        self.assertEqual(items["logs"]["bytes"], 100)
        self.assertEqual(dir_bytes(tmp), 100)


if __name__ == "__main__":
    unittest.main()
