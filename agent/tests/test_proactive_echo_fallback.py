#!/usr/bin/env python3
"""主動訊息一定要落進她自己的線（owner 2026-11-04：「她發之前有沒有確認之前發過什麼？」）。

實測症狀：最近 10 條主動訊息只有 2 條出現在任何 thread 的 transcript 裡，其餘 8 條只存在
`proactive.json`／`outbox`／activity log ⇒ 她下一輪看不到自己剛說過什麼，回起來就像「清空腦子」。

根因：`proactive_echo.record()` 只在**開著的**私聊線裡挑一條寫；線過窗被 reap 之後就沒有候選，
直接 return False（靜默丟棄）。修正：沒有候選就**開一條**再寫（scope 要跟她平常那條一致）。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from agent.brain.compose.proactive_echo import make_proactive_recorder
from agent.brain.runtime.threadstore import ThreadStore


class RecorderFallsBackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.store = ThreadStore(path=self.root / "threads.json")
        self.record = make_proactive_recorder(self.store)

    def test_no_open_thread_still_records(self):
        self.assertTrue(self.record("mira", "owner", "おはよう、ちゃんと起きてる？", 1000.0))
        threads = list(self.store.threads())
        self.assertTrue(threads, "應該要開出一條線")
        said = [m.text for t in threads for m in t.transcript]
        self.assertIn("おはよう、ちゃんと起きてる？", said)

    def test_second_proactive_goes_to_the_same_line(self):
        self.record("mira", "owner", "一回目", 1000.0)
        self.record("mira", "owner", "二回目", 1005.0)
        threads = list(self.store.threads())
        self.assertEqual(len(threads), 1, "同一條線應該重用，不要每次開新的")
        said = [m.text for t in threads for m in t.transcript]
        self.assertEqual(said, ["一回目", "二回目"])


if __name__ == "__main__":
    unittest.main()
