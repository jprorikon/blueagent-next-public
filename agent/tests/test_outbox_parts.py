#!/usr/bin/env python3
"""回覆分段：後台「發出即落檔」＋補拿／歷史畫得出同樣的分段（owner 2026-11-04）。

病徵（兩條，同一個根）：
  ① 串流時她是「一條一條短訊息」（SSE `part` 幀，server 切句），
     但 `GET /api/turns` 補拿／歷史只回全文 ⇒ 同一輪補回來變成一大條氣泡；
  ② 整輪的紀錄以前只在**收尾**才寫（還卡在語音合成之後）⇒ 逾時／斷線／重啟，
     她已經說出口的那幾句一句都不留。

判據：
  ① `part` 一發出去就落一筆（`note_part`），不等語音、不看有沒有聽完；
  ② 收尾匯總成正式那一列（`parts`），`/api/turns` 原樣回出去；
  ③ 沒跑完但已經有句子 ⇒ 照樣落一列，標 `ok=False`；
  ④ 連一句都沒有 ⇒ 不寫（不要留空殼）；
  ⑤ 程序在收尾前被重啟 ⇒ `recover_pending()` 把分句檔補成正式列。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.streaming import StreamDelivery
from agent.brain.compose.turns import reply_bubbles
from agent.brain.runtime.delivery.outbox import TurnOutbox


def _result(text: str, bubbles: list[str] | None = None, finished=True, ok=True) -> dict:
    payload = {"text": text, "entity": "mira", "principal": "owner"}
    if bubbles is not None:
        payload["bubbles"] = bubbles
    return {"ok": ok, "finished": finished, "source": "phone:owner",
            "decision": {"thread": "th_1"},
            "frames": [{"kind": "part", "payload": {"index": 0, "text": text}},
                       {"kind": "done", "payload": payload}]}


class TestReplyBubbles(unittest.TestCase):
    def test_reads_segments_from_done_frame(self):
        result = _result("一。二。三。", ["一。", "二。", "三。"])
        self.assertEqual(reply_bubbles(result), ["一。", "二。", "三。"])

    def test_missing_bubbles_is_empty_not_error(self):
        self.assertEqual(reply_bubbles(_result("一。")), [])


class TestPartsInOutbox(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.box = TurnOutbox(self.root, keep=10)
        self.delivery = StreamDelivery(send=lambda **_: _result(""), outbox=self.box)

    def _kwargs(self) -> dict:
        return {"principal": "owner", "entity": "mira", "source_override": "phone:owner"}

    def _save(self, turn_id: str, result: dict, reply: str = "") -> None:
        self.delivery.save(turn_id, result, [], {"frames": 1, "speech": 0}, self._kwargs(),
                           reply=reply or result["frames"][-1]["payload"]["text"],
                           bubbles=reply_bubbles(result))

    # ── ① 發出即落檔 ─────────────────────────────────────────────────────
    def test_note_part_writes_immediately_and_is_taken_by_save(self):
        parts = ["おはよう。", "今日は寒いね。"]
        for piece in parts:
            self.box.note_part("t1", piece, meta=self._kwargs())
        self.assertTrue(self.box.parts_path("t1").exists(), "一句話發出去就該有檔")
        self._save("t1", _result("".join(parts)), reply="".join(parts))
        row = self.box.get("t1", principal="owner")
        self.assertEqual(row["parts"], parts)
        self.assertEqual(row["text"], "おはよう。今日は寒いね。")
        self.assertFalse(self.box.parts_path("t1").exists(), "收尾後分句檔要收走")

    def test_parts_come_from_file_even_without_done_bubbles(self):
        self.box.note_part("t2", "一句。", meta=self._kwargs())
        self.box.note_part("t2", "兩句。")
        self._save("t2", _result("一句。兩句。"), reply="一句。兩句。")
        self.assertEqual(self.box.get("t2", principal="owner")["parts"], ["一句。", "兩句。"])

    # ── ② 單條不寫 parts（客戶端照舊畫一條）────────────────────────────
    def test_single_bubble_writes_no_parts(self):
        self._save("t3", _result("うん。", ["うん。"]))
        row = self.box.get("t3", principal="owner")
        self.assertNotIn("parts", row)
        self.assertEqual(row["text"], "うん。")

    # ── ③ 沒跑完但已經有句子 ⇒ 落一列、標不完整 ─────────────────────────
    def test_unfinished_turn_with_parts_is_recorded(self):
        self.box.note_part("t4", "まだ途中だけど。", meta=self._kwargs())
        self._save("t4", _result("", finished=False, ok=False))
        row = self.box.get("t4", principal="owner")
        self.assertIsNotNone(row, "她已經說出口的句子不能丟")
        self.assertEqual(row["text"], "まだ途中だけど。")
        self.assertFalse(row["ok"])
        self.assertEqual(row["error"], "incomplete")

    # ── ④ 連一句都沒有 ⇒ 不寫空殼 ───────────────────────────────────────
    def test_unfinished_turn_without_parts_is_not_recorded(self):
        self._save("t5", _result("", finished=False, ok=False))
        self.assertIsNone(self.box.get("t5", principal="owner"))

    # ── ⑤ 重啟對帳：分句檔補成正式列 ────────────────────────────────────
    def test_recover_pending_materialises_orphan_parts(self):
        self.box.note_part("t6", "一句。", meta=self._kwargs())
        self.box.note_part("t6", "兩句。")
        self.assertEqual(self.box.recover_pending(), 1)
        row = self.box.get("t6", principal="owner")
        self.assertIsNotNone(row)
        self.assertEqual(row["parts"], ["一句。", "兩句。"])
        self.assertEqual(row["principal"], "owner")
        self.assertFalse(row["ok"])
        self.assertFalse(self.box.parts_path("t6").exists())
        self.assertEqual(self.box.recover_pending(), 0, "再跑一次不該重複補")

    def test_recover_pending_skips_turns_already_recorded(self):
        self.box.note_part("t7", "一句。", meta=self._kwargs())
        self._save("t7", _result("一句。", ["一句。"]))
        self.assertEqual(self.box.recover_pending(), 0)


    def test_single_sticker_keeps_asked_parts(self):
        """單張貼圖／單張照片也要落 `asked_parts`（延時聚合之後不再走 legacy 的 `asked_sticker`）。"""
        row = self.box.record(turn_id="t_s1", principal="owner", entity="mira",
                                 text="うん", asked="",
                                 asked_parts=[{"kind": "sticker", "sticker": "smile", "text": ""}])
        self.assertEqual(row.get("asked_parts"),
                         [{"kind": "sticker", "sticker": "smile", "text": ""}])

    def test_single_plain_text_is_not_stored(self):
        row = self.box.record(turn_id="t_s2", principal="owner", entity="mira",
                                 text="うん", asked="一行だけ",
                                 asked_parts=[{"kind": "text", "text": "一行だけ"}])
        self.assertNotIn("asked_parts", row)


    def test_sticker_marker_lands_in_the_text(self):
        """她回貼圖 ⇒ outbox 正文附英文標記 `[[sticker:<id>]]`（前台照它渲染，owner 2026-11-04）。"""
        self.delivery.save("t_mark", {"finished": True, "source": "phone:owner",
                                      "text": "はい", "sticker_ids": ["000_hellooo"]},
                           [], {}, self._kwargs(), reply="はい")
        row = self.box.get("t_mark")
        self.assertEqual(row.get("text"), "はい [[sticker:000_hellooo]]")


if __name__ == "__main__":
    unittest.main(verbosity=2)
