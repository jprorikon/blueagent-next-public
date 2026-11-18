#!/usr/bin/env python3
"""`runtime/reply_split` 與共用夾具逐欄對拍（2026-10-23）。

P7-① 把回覆分段搬到 server 之後，端末只照 `part` 幀顯示；這份 Python 實作若與
`ReplySplit.kt` / `ReplySplit.cs` 不同，氣泡邊界與朗讀節奏就會走鐘。
`app/client/fixtures/reply_split.json` 是 Kotlin 側機器產生的真值（不是手寫），
所以這裡逐條比 groups／sentences／chars／speakable／boundsMs。
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.brain.runtime import reply_split  # noqa: E402

FIXTURE = ROOT / "app" / "client" / "fixtures" / "reply_split.json"


class TestReplySplitParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FIXTURE.exists():
            # Docker image 只 copy 執行期需要的目錄（沒有 app/client/），對拍自動跳過
            raise unittest.SkipTest(f"找不到共用夾具：{FIXTURE}")
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"), strict=False)
        cls.cases = cls.fixture["cases"]

    def test_fixture_is_not_empty(self):
        self.assertGreater(len(self.cases), 20, "fixture 太少（匯出那支是不是忘了加 case）")

    def test_every_case_matches_kotlin(self):
        for case in self.cases:
            with self.subTest(case=case["name"]):
                data = case["in"]
                expected = case["out"]
                text, maximum = data["text"], data["max"]
                got_groups = reply_split.groups(text, maximum)
                self.assertEqual(got_groups, expected["groups"], "groups 不同")
                self.assertEqual(reply_split.sentences(text),
                                 expected["sentences"], "sentences 不同")
                self.assertEqual(reply_split.chars(text),
                                 expected["chars"], "chars 不同")
                self.assertEqual(reply_split.speakable(text),
                                 expected["speakable"], "speakable 不同")
                self.assertEqual(reply_split.bounds_ms(got_groups, data["msPerChar"]),
                                 expected["boundsMs"], "boundsMs 不同")

    def test_only_last_bubble_is_held_while_streaming(self):
        text = "一。二。三。四。五。六。"
        ready = reply_split.groups(text)
        self.assertEqual(len(ready), 2)
        self.assertEqual(reply_split.ready_count(ready, final=False), 1)
        self.assertEqual(reply_split.ready_count(ready, final=True), 2)
        self.assertEqual(reply_split.ready_count([], final=False), 0)
        self.assertEqual(reply_split.ready_count([], final=True), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
