#!/usr/bin/env python3
"""窗口摘要的復讀過濾（owner 2026-11-03 第 4 步）。

判據：
  ① 同一句在一窗裡重複到 `REPEAT_LIMIT` 次 ⇒ 不提煉（`repetitive`）——
     實測 nori 被 guest-x 同一句問 16 次、她回 60 輪同一句，摘要池因此被垃圾佔滿；
  ② 整窗只有一兩種不同的話 ⇒ 沒有資訊可抽（`no_content`）；
  ③ 正常的長窗口**不能**被誤殺（TRPG 播報那種多輪推進要留著）。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.runtime.distill import REPEAT_LIMIT, Distiller  # noqa: E402


def _session(texts: list[str]) -> dict:
    return {"rows": [{"text": text, "speaker": "guest-x", "turn": index}
                     for index, text in enumerate(texts)],
            "turns": len(texts), "entity": "nori"}


class TestWindowGates(unittest.TestCase):
    def test_normal_window_is_kept(self):
        texts = ["ノリ、今日の観測はどう？", "仕組みの提案を三行で書いてみた。",
                 "それは面白いね。どこが肝なの？", "待ち時間をなくすところ。",
                 "なるほど、数字で見せられる？", "三行目に指標を足すよ。",
                 "いいね、明日までにまとめて。", "了解、夕方には出す。"]
        keep, reason = Distiller.keep_by_rules(_session(texts))
        self.assertTrue(keep, reason)

    def test_same_line_repeated_is_rejected(self):
        line = "ねえ、今日って何かいいことあった？"
        texts = ["ノリ、今日の観測はどう？"] + [line] * 12
        keep, reason = Distiller.keep_by_rules(_session(texts))
        self.assertFalse(keep)
        self.assertEqual(reason, "repetitive")
        self.assertGreaterEqual(REPEAT_LIMIT, 3)

    def test_only_two_distinct_lines_is_rejected(self):
        texts = ["ノリ、今日の観測はどうなっているの？", "この問題については少し考える時間が必要です。"] * 6
        keep, reason = Distiller.keep_by_rules(_session(texts))
        self.assertFalse(keep)
        self.assertIn(reason, {"repetitive", "no_content"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
