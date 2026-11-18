#!/usr/bin/env python3
"""Raw 原文倉（`runtime/rawlog.py`）：按角色分倉、append-only、12 個月保留。

判據：
  ① 一輪一行落進 `data/raw/<entity>/<YYYY-MM>.jsonl`，且 `room` 含她自己；
  ② 重複 sync 不會重複寫（watermark）；
  ③ 保留期到就刪月檔（預設 12 個月）；
  ④ 檢索按二字重疊，且**只回房間成員 ⊆ 在場的人**那些輪次（披露層）。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.thread import MemoryScope, ThreadKey, TurnRecord
from agent.brain.runtime.rawlog import RawLog


class FakeThread:
    def __init__(self, entity: str, thread_id: str, participants, turns):
        self.key = ThreadKey(scope="owner", entity=entity, scene="chat", purpose="talk")
        self.id = thread_id
        self.participants = set(participants)
        self.segment = list(turns)


def turn(turn_id: str, text: str, *, at: float, role: str = "user") -> TurnRecord:
    return TurnRecord(turn_id=turn_id, role=role, text=text, source="app", at=at,
                      speaker="owner" if role == "user" else "mira")


class TestRawLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.log = RawLog(self.root, keep_months=12)

    def test_appends_one_line_per_turn_with_room(self):
        thread = FakeThread("mira", "th-1", {"owner"}, [
            turn("t1", "今、小説読んでるよ。", at=time.time()),
            turn("t2", "へえ、小説？", at=time.time(), role="assistant"),
        ])
        self.assertEqual(self.log.sync_thread(thread), 2)
        files = list((self.root / "mira").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["turn"] for row in rows], ["t1", "t2"])
        self.assertIn("mira", rows[0]["room"])
        self.assertIn("owner", rows[0]["room"])

    def test_watermark_prevents_duplicates(self):
        thread = FakeThread("mira", "th-1", {"owner"},
                            [turn("t1", "第一句", at=time.time())])
        self.log.sync_thread(thread)
        self.assertEqual(self.log.sync_thread(thread), 0)
        thread.segment.append(turn("t2", "第二句", at=time.time()))
        self.assertEqual(self.log.sync_thread(thread), 1)
        self.assertEqual(self.log.stats("mira")["turns"], 2)

    def test_rotation_drops_months_beyond_keep_window(self):
        folder = self.root / "mira"
        folder.mkdir(parents=True)
        now = time.time()
        year, month = time.localtime(now).tm_year, time.localtime(now).tm_mon
        index = year * 12 + month - 1
        old_year, old_month = (index - 13) // 12, (index - 13) % 12 + 1
        old = folder / f"{old_year:04d}-{old_month:02d}.jsonl"
        old.write_text("", encoding="utf-8")
        removed = self.log.rotate(now=now)
        self.assertIn(str(old), removed)
        self.assertFalse(old.exists())

    def test_search_respects_room(self):
        thread = FakeThread("mira", "th-1", {"owner"}, [
            turn("t1", "主人在读佐々木とピーちゃん，读到第六卷", at=time.time()),
        ])
        self.log.sync_thread(thread)
        hit = self.log.search("mira", "佐々木", room={"mira", "owner"})
        self.assertTrue(hit)
        miss = self.log.search("mira", "佐々木", room={"mira", "bob"})
        self.assertEqual(miss, [])

    def test_bins_are_per_entity(self):
        self.log.sync_thread(FakeThread("mira", "th-1", {"owner"},
                                       [turn("t1", "ミラの記憶", at=time.time())]))
        self.log.sync_thread(FakeThread("aster", "th-2", {"owner"},
                                       [turn("t1", "アスターの記憶", at=time.time())]))
        self.assertEqual(self.log.stats("mira")["turns"], 1)
        self.assertEqual(self.log.stats("aster")["turns"], 1)
        self.assertEqual(self.log.search("aster", "ミラ"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
