#!/usr/bin/env python3
"""raw 水準（2026-11-01）與回填工具的回歸。

實測到的壞況（nori，2026-11-01）：`rawlog.sync_thread()` 舊版水準只記 `turn_id`，而**一輪有兩條
記錄**（`user`／`assistant` 共用同一個 `turn_id`、`at` 差幾秒）：
  ① 水準對得上時，`ids.index(last) + 1` 只跳過該 id 的**第一條** ⇒ 同輪的 assistant 條**再寫一遍**
     （raw 裡「同一輪寫 9 次」就是這樣來的）；
  ② segment 被封存搬走後，水準那個 id 不在 segment 裡 ⇒ `turns = []` **整條線永久停寫**
     （11:29–11:45 九輪、含「池袋あたりに電器店…」就此消失，於是主動訊息只記得「午後は出る」）。

判據：
  ① 同輪兩條只寫一次；重複 sync 冪等；
  ② 水準失效（segment 換過）**不再放棄**，而且會跟既有 raw 比對（不重複）；
  ③ 舊水準（沒有 `@at`）指到 segment 中間 ⇒ 只寫它之後的；
  ④ 回填工具：dry-run 不寫、`--apply` 補齊、再跑一次是 0（冪等）。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))   # 倉庫根

from agent.brain.domain.thread import ThreadKey, TurnRecord  # noqa: E402
from agent.brain.runtime.rawlog import AT_SUFFIX, RawLog  # noqa: E402

LINE = ThreadKey(scope="owner", entity="nori", scene="chat", purpose="chat")
THREAD_ID = LINE.with_generation(13)


class FakeThread:
    def __init__(self, turns, participants=("owner",), thread_id=THREAD_ID):
        self.key = LINE
        self.id = thread_id
        self.participants = set(participants)
        self.segment = list(turns)


def turn(turn_id, text, at, role="user", speaker="owner"):
    return TurnRecord(turn_id=turn_id, role=role, text=text, source="phone:owner",
                      at=at, speaker=speaker)


def round_of(turn_id, question, answer, at):
    """一輪＝兩條記錄，共用同一個 turn_id（線上就是這個形狀）。"""
    return [turn(turn_id, question, at),
            turn(turn_id, answer, at + 7.0, role="assistant", speaker="")]


def rows_on_disk(raw, entity="nori"):
    out = []
    for path in sorted(raw.entity_dir(entity).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


class TestRawWatermark(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw = RawLog(pathlib.Path(self.tmp.name) / "raw")
        self.t0 = 1793500057.0

    def test_one_round_two_records_written_once(self):
        """同輪的 user＋assistant 兩條都要寫，而且只寫一次。"""
        thread = FakeThread(round_of("t_a", "ちょっと午後は出るかな", "午後、どこへ出るんですか。", self.t0))
        self.assertEqual(self.raw.sync_thread(thread), 2)
        self.assertEqual(self.raw.sync_thread(thread), 0)          # 再同步一次：沒有新的
        self.assertEqual(len(rows_on_disk(self.raw)), 2)
        self.assertEqual(self.raw._load_watermark("nori").get(THREAD_ID + AT_SUFFIX),
                         f"{self.t0 + 7.0:.6f}")

    def test_stale_watermark_does_not_stop_the_line(self):
        """水準那一輪已被 segment 搬走 ⇒ 仍然寫，且不重複已經在 rc 裡的那些。"""
        first = FakeThread(round_of("t_a", "A", "a", self.t0))
        self.raw.sync_thread(first)
        # segment 換過：只剩新的兩輪，水準指的那一輪已經不在了（模擬封存搬走）
        later = FakeThread(round_of("t_b", "池袋あたりに電器店まわりでもしようかと", "電器店、いいですね。", self.t0 + 180)
                           + round_of("t_c", "パソコンのパーツとか", "パーツ、いいですね。", self.t0 + 300))
        self.assertEqual(self.raw.sync_thread(later), 4)
        texts = [row["text"] for row in rows_on_disk(self.raw)]
        self.assertIn("池袋あたりに電器店まわりでもしようかと", texts)
        self.assertEqual(len(rows_on_disk(self.raw)), 6)           # 舊的 2 ＋ 新的 4，沒有重複

    def test_legacy_watermark_without_at_slices_after_last_record(self):
        """舊水準（只有 turn_id）指到 segment 中間 ⇒ 只寫它之後的，不重寫同一輪的第二條。"""
        thread = FakeThread(round_of("t_a", "A", "a", self.t0)
                             + round_of("t_b", "B", "b", self.t0 + 100))
        self.raw._save_watermark("nori", {THREAD_ID: "t_a"})        # 舊格式：只有 turn_id
        self.assertEqual(self.raw.sync_thread(thread), 2)          # 只該寫 t_b 的兩條
        self.assertEqual([row["turn"] for row in rows_on_disk(self.raw)], ["t_b", "t_b"])


class TestBackfillFromThreads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = pathlib.Path(self.tmp.name)
        (self.data / "raw" / "nori").mkdir(parents=True)
        self.thread = {
            "id": THREAD_ID, "key": LINE.to_dict(), "participants": ["owner"],
            "segment": [], "transcript": [],
        }
        self.rounds = [round_of("t_a", "今日天気いいね", "ええ、いい天気ですね。", 1793499976.0),
                       round_of("t_b", "池袋あたりに電器店まわりでもしようかと", "電器店、いいですね。", 1793500151.0)]
        self.thread["transcript"] = [turn.to_dict() for chunk in self.rounds for turn in chunk]
        (self.data / "threads.json").write_text(json.dumps({"threads": [self.thread]}), encoding="utf-8")
        # raw 裡已經有第一輪
        for item in self.rounds[0]:
            line = json.dumps({"at": item.at, "entity": "nori", "turn": item.turn_id,
                               "thread": THREAD_ID, "scene": "chat", "role": item.role,
                               "speaker": item.speaker, "addressed_to": "", "source": item.source,
                               "room": ["nori", "owner"], "text": item.text, "audio": "",
                               "engage": "", "generation": 13}, ensure_ascii=False)
            with (self.data / "raw" / "nori" / "2026-11.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _run(self, *args):
        script = HERE.parent / "tools" / "backfill_raw_from_threads.py"
        out = subprocess.run([sys.executable, str(script), "--data", str(self.data), *args],
                             capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_dry_run_writes_nothing(self):
        before = (self.data / "raw" / "nori" / "2026-11.jsonl").read_text(encoding="utf-8")
        out = self._run()
        self.assertIn("缺 2", out)
        self.assertEqual((self.data / "raw" / "nori" / "2026-11.jsonl").read_text(encoding="utf-8"), before)

    def test_apply_fills_and_is_idempotent(self):
        self._run("--apply")
        text = (self.data / "raw" / "nori" / "2026-11.jsonl").read_text(encoding="utf-8")
        self.assertIn("池袋", text)
        self.assertEqual(text.count('"turn": "t_b"'), 2)          # 一輪兩條，各一次
        out = self._run()                                          # 再跑一次
        self.assertIn("缺 0", out)


if __name__ == "__main__":
    unittest.main()
