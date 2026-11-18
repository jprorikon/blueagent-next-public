#!/usr/bin/env python3
"""行動日誌 → 記憶的沉澱規則（owner 2026-10-24）。

為什麼要有這支：她的主動訊息曾經被寫成**通用事實**（entity=''、永久有效），
結果 ① 自己的話回灌給自己（同一件「角色卡」講四次）② 別的角色也吃得到（nori 照抄同一句）。

判據：
  ① 她對人說的話（heartbeat／voice／trpg-report）**不進記憶**
  ② 真正的行動（study 之類）進記憶時要綁 entity，而且是**事件**（有失效時間）
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))   # 倉庫根

from demo_common import *  # noqa: F401,F403


class TestActivitySink(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ba-actsink-")
        self.app = build(brain_delay=0, data_dir=self.root,
                          step_duration=0.02, life_pause=0.005)
        self.addCleanup(self.app.shutdown_lives)
        self.log = self.app.activity_log

    def facts(self, needle: str) -> list[dict]:
        body = self.app.memory.snapshot(entity="mira")   # 不過披露層，看她的全量
        return [row for row in body.get("facts") or [] if needle in str(row.get("text") or "")]

    def test_conversational_kinds_are_not_memorised(self):
        self.log.append("mira", kind="heartbeat", visibility="public",
                        text="オーナー、役割カード、今から始めますか？（測試心跳）")
        self.assertEqual(self.facts("測試心跳"), [],
                         "她對主人說的話不該變成記憶（會自我強化）")

    def test_real_activity_is_bound_to_entity_and_expires(self):
        self.log.append("mira", kind="study", visibility="public",
                        text="翻規則書，找三條以前沒用過的判定（測試行動）")
        rows = self.facts("測試行動")
        self.assertTrue(rows, "真正的行動應該沉進記憶")
        row = rows[0]
        self.assertEqual(row.get("entity"), "mira",
                         "要綁在她自己身上（entity='' 會污染別的角色）")
        # 事件型 ⇒ 有失效時間（`to_dict` 不帶 valid_to，直接看記憶體裡的物件）
        inner = [fact for fact in self.app.memory._facts            # noqa: SLF001 - 測試要看真值
                 if "測試行動" in fact.text]
        self.assertTrue(inner, "記憶體裡應該有這一條")
        self.assertEqual(inner[0].kind, "event")
        self.assertGreater(inner[0].valid_to, 0, "事件型要有失效時間，不是永久事實")
        self.assertAlmostEqual(inner[0].valid_to - inner[0].valid_from, 3 * 86400, delta=1)   # owner 2026-11-16：1 → 3 天


if __name__ == "__main__":
    unittest.main(verbosity=2)
