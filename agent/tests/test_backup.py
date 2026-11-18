#!/usr/bin/env python3
"""每日資料快照（`runtime/ops/backup.py`）：她的記憶只有一份，先加保險。

判據：
  ① 該備的都備到（facts.db／threads.json／raw／windows／prethink／users）；
  ② 同一天再跑＝覆蓋同一份，不會愈備愈多；
  ③ 保留期到就整包刪掉（預設 7 天）；
  ④ 太大的檔跳過並記在 MANIFEST（不要為了備份把磁碟塞爆）；
  ⑤ 缺檔不炸（新建環境什麼都沒有也要能跑）。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.ops.backup import DataBackup


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = pathlib.Path(self.tmp.name)
        (self.data / "memory").mkdir(parents=True)
        (self.data / "memory" / "facts.db").write_bytes(b"sqlite-bytes")
        (self.data / "threads.json").write_text('{"threads": []}', encoding="utf-8")
        (self.data / "raw" / "mira").mkdir(parents=True)
        (self.data / "raw" / "mira" / "2026-10.jsonl").write_text(
            '{"text":"第一句"}\n', encoding="utf-8")
        (self.data / "memory" / "windows" / "mira").mkdir(parents=True)
        (self.data / "memory" / "windows" / "mira" / "w1.json").write_text(
            '{"summary":"在讀小說"}', encoding="utf-8")
        (self.data / "prethink").mkdir()
        (self.data / "prethink" / "mira.json").write_text('{"note":"預想"}', encoding="utf-8")
        (self.data / "users").mkdir()
        (self.data / "users" / "owner.md").write_text("# 主人", encoding="utf-8")
        self.backup = DataBackup(self.data)

    def test_runs_and_copies_everything(self):
        result = self.backup.run_once(now=time.time())
        self.assertTrue(result["ok"])
        folder = self.data / "backups" / result["date"]
        self.assertTrue((folder / "memory" / "facts.db").exists())
        self.assertTrue((folder / "threads.json").exists())
        self.assertTrue((folder / "raw" / "mira" / "2026-10.jsonl").exists())
        self.assertTrue((folder / "memory" / "windows" / "mira" / "w1.json").exists())
        self.assertTrue((folder / "prethink" / "mira.json").exists())
        self.assertTrue((folder / "users" / "owner.md").exists())
        manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"], result["files"])

    def test_same_day_overwrites_not_duplicates(self):
        now = time.time()
        self.backup.run_once(now=now)
        self.backup.run_once(now=now)
        folders = [path for path in (self.data / "backups").iterdir() if path.is_dir()]
        self.assertEqual(len(folders), 1)

    def test_prunes_old_backups(self):
        now = time.time()
        old = self.data / "backups" / time.strftime("%Y-%m-%d", time.localtime(now - 30 * 86400))
        old.mkdir(parents=True)
        result = self.backup.run_once(now=now)
        self.assertIn(old.name, result["pruned"])
        self.assertFalse(old.exists())

    def test_oversize_file_is_skipped(self):
        big = self.data / "memory" / "windows" / "mira" / "big.json"
        big.write_bytes(b"x" * 2048)
        backup = DataBackup(self.data, max_file_mb=0.001)     # ≈1KB
        result = backup.run_once(now=time.time())
        self.assertTrue(any(item["path"].endswith("big.json") for item in result["skipped"]),
                        result["skipped"])
        self.assertGreater(result["files"], 0)

    def test_empty_dir_does_not_crash(self):
        empty = pathlib.Path(self.tmp.name) / "empty"
        empty.mkdir()
        result = DataBackup(empty).run_once(now=time.time())
        self.assertTrue(result["ok"])
        self.assertEqual(result["files"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
