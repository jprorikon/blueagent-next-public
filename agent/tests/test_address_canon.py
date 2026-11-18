#!/usr/bin/env python3
"""稱呼收斂的判據（owner 2026-11-03「收斂到 オーナー」）。

判據：
  ① 稱呼句（人稱詞與稱呼動詞相鄰）⇒ 一律改寫成權威一句；
  ② 只是「句子裡剛好提到オーナー／船長」的長句、或沒有人稱的句子 ⇒ **不動**（誤傷會毀掉日誌）；
  ③ 同一句改寫後 ⇒ 同一 norm ⇒ 記憶庫自己擋重複（不會再長第二條）。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.memory.fact_rules import (ADDRESS_CANON, ADDRESS_SLOT,  # noqa: E402
                                              canonical_address, is_address)
from agent.brain.memory.facts import normalize_text  # noqa: E402

YES = [
    "她稱呼主人為「船長」",
    "主人のことは「船長」と呼ぶ（以前「オーナー」と呼んだが訂正された）",
    "主人は彼女から「船長」と呼ばれる",
    "owner称呼ミラ为オーナー",
    "助手以日文稱呼主人為「オーナー」。",
    "她希望被叫「オーナー」",
]
NO = [
    "ゲストの依頼はオーナーに回すのが彼女の対応方針。",
    "主人（船長）很怕冷，睡覺時要好好蓋被子。",
    "主人说下周要考试，最近有点紧张。",
    "アスター「ほかの二人が最近何をしているか見る」2/3 ノリの最近の公開記録を読む："
    "ノリの最近の公開記録をたどると、「仕組みの提案を三行で書く」という4段の取り組みが残っていた。",
    "",
]


class TestAddressRule(unittest.TestCase):
    def test_address_sentences_are_recognized(self):
        for text in YES:
            with self.subTest(text=text):
                self.assertTrue(is_address(text))

    def test_incidental_mentions_are_left_alone(self):
        for text in NO:
            with self.subTest(text=text):
                self.assertFalse(is_address(text))
                self.assertEqual(canonical_address(text), text)

    def test_every_shape_maps_to_one_canonical_sentence(self):
        for text in YES:
            with self.subTest(text=text):
                self.assertEqual(canonical_address(text), ADDRESS_CANON)
        # 同一句 ⇒ 同一 norm ⇒ add() 會擋重複（這是「不會再長第二條」的機制）
        self.assertEqual(len({normalize_text(canonical_address(t)) for t in YES}), 1)

    def test_canonical_slot_is_pinned(self):
        self.assertEqual(ADDRESS_SLOT, "address")
        self.assertIn("オーナー", ADDRESS_CANON)


if __name__ == "__main__":
    unittest.main(verbosity=2)
