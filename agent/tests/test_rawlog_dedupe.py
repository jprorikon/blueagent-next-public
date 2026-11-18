#!/usr/bin/env python3
"""原文倉（raw）重複寫入的回歸（2026-10-24）。

實測到的壞況：`data/raw/` 9369 行只有 **157 個不同的 turn**，其中一條「おはよう、今日の調子は？」
被寫了 **169 次**（同一秒內）。成因＝水印（`.watermark.json`）是「讀→追加→寫」的
讀改寫，兩個執行者互相蓋掉對方的新水印 ⇒ 下一輪又把整段重寫一次。

判據：
  ① 水印檔被刪掉（或別人蓋掉）後再同步，**不會**重複 append（本程序記住寫過的）
  ② 舊檔裡已經有重複行時，讀出來只有一份（檢索／提煉不被重複行灌爆）
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.domain.thread import ThreadKey, TurnRecord  # noqa: E402
from agent.brain.runtime.rawlog import RawLog  # noqa: E402

LINE = ThreadKey(scope="owner", entity="mira", scene="chat", purpose="chat")


class FakeThread:
    def __init__(self, key, generation, participants, turns):
        self.key = key
        self.id = key.with_generation(generation)
        self.participants = set(participants)
        self.segment = list(turns)


def turn(turn_id, text, at):
    return TurnRecord(turn_id=turn_id, role="user", text=text, source="app", at=at,
                      speaker="owner")


class TestRawLogDedupe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.raw = RawLog(self.root / "raw")

    def lines(self) -> list[dict]:
        rows: list[dict] = []
        for path in (self.root / "raw").glob("*/*.jsonl"):
            rows.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
        return rows

    def test_lost_watermark_does_not_duplicate(self):
        thread = FakeThread(LINE, 1, {"owner"}, [turn("t1", "おはよう", 1000.0)])
        self.assertEqual(self.raw.sync_thread(thread), 1)
        # 水印被別的程序蓋掉／刪掉 ⇒ 舊行為會把整段再寫一次
        (self.root / "raw" / "mira" / ".watermark.json").unlink()
        self.assertEqual(self.raw.sync_thread(thread), 0, "本程序寫過的不該再寫")
        self.assertEqual(len(self.lines()), 1)

    def test_reading_old_duplicates_returns_one(self):
        folder = self.root / "raw" / "mira"
        folder.mkdir(parents=True)
        row = {"at": 1000.0, "entity": "mira", "turn": "t1", "thread": "th_x-1",
               "role": "user", "speaker": "owner", "text": "おはよう", "room": ["mira", "owner"]}
        with (folder / "2026-10.jsonl").open("w", encoding="utf-8") as handle:
            for _ in range(5):                     # 舊檔裡的重複行
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        rows = list(self.raw._iter_rows("mira"))        # noqa: SLF001 - 測試要看真值
        self.assertEqual(len(rows), 1, "同一個 turn 只該出現一次")
        self.assertEqual(len(self.raw.recent("mira", limit=10)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
