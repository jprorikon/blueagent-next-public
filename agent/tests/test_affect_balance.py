#!/usr/bin/env python3
"""情緒量表修正（owner 2026-10-24：「我總覺得她一直挺開心的」→ 三件一起做）。

實測到的三個結構問題：
  ① delta_valence 幾乎只有正的（24h 126 次評分，負的 3%、frustration 0 次）
  ② delta_arousal = max(0, intensity) * 0.8 ⇒ **永遠不可能下降**
  ③ 98/126 次是同一檔 `interest +0.18/+0.28` ⇒ 同一個推力每輪重複，基線一路被墊高

這支守四件事：
  A. 負面種類（concern／frustration／uncertainty）一定把 valence 拉下來
  B. arousal 可以是負的（強度低＝平靜下來）
  C. 連續同種類同幅度 ⇒ 折半（多樣性保護）
  D. 政策會就地遷移（`project()` 讀的是存檔裡的 policy，只改程式預設值對舊 store 沒用）
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.runtime.inner.affect import (  # noqa: E402
    DEFAULT_POLICY, EVENT_AFFECT, AffectService)
from agent.brain.vendor import aem_v2  # noqa: E402


def service() -> AffectService:
    return AffectService(pathlib.Path(tempfile.mkdtemp()))


def episodes(svc: AffectService, entity: str = "mira") -> list[dict]:
    return svc.timeline(entity, limit=50)


class TestNegativeAppraisals(unittest.TestCase):
    def test_rejection_makes_valence_go_down(self):
        svc = service()
        svc.note_message("mira", "それ、やめて")          # 主人叫停
        rows = episodes(svc)
        self.assertTrue(rows, "應該有記到一筆評分")
        self.assertLess(rows[0]["delta_valence"], 0)

    def test_interrupted_is_negative(self):
        svc = service()
        svc.note_message("mira", "ねえ聞いて")
        result = svc.interrupted("mira")
        self.assertTrue(result.get("ok"), result)
        negatives = [row for row in episodes(svc) if row["delta_valence"] < 0]
        self.assertTrue(negatives, "被打斷要留一筆負增量")

    def test_negative_kinds_override_positive_valence(self):
        svc = service()
        noted = svc.note_message("mira", "……")
        svc.note_state("mira", {"emotion": {"label": "心配", "intensity": 0.6,
                                              "valence": 0.8}},   # 模型給正的也不該往上
                       source_event_id=str(noted["event_id"]))
        rows = episodes(svc)
        self.assertTrue(rows)
        self.assertLessEqual(rows[0]["delta_valence"], 0,
                             f"concern 應該往下：{rows[0]}")


class TestArousalCanFall(unittest.TestCase):
    def test_low_intensity_does_not_push_arousal_up(self):
        """AEM 的 schema 規定 `delta_a ∈ [0,1]` ⇒ 「平靜」只能給 0（靠衰減退掉）。"""
        svc = service()
        noted = svc.note_message("mira", "……")
        svc.note_state("mira", {"emotion": {"label": "穏やか", "intensity": 0.1,
                                              "valence": 0.2}},
                       source_event_id=str(noted["event_id"]))
        rows = episodes(svc)
        self.assertEqual(rows[0]["delta_arousal"], 0.0, "低強度不該再把 arousal 往上推")

    def test_arousal_half_life_is_shortened(self):
        self.assertEqual(DEFAULT_POLICY["half_life_min"]["a"], 60,
                         "興奮要一小時內退掉（schema 不給負增量，只能縮短半衰期）")


class TestDiversityDampen(unittest.TestCase):
    def test_fourth_identical_appraisal_is_halved(self):
        svc = service()
        values = [svc._dampen("mira", "interest", 0.18, 0.28) for _ in range(4)]
        self.assertEqual(values[0], (0.18, 0.28))
        self.assertEqual(values[3], (0.09, 0.14), "連續同值第四次要折半")

    def test_different_kinds_are_not_dampened(self):
        svc = service()
        for kind in ("interest", "concern", "pleasure", "interest"):
            svc._dampen("mira", kind, 0.18, 0.28)
        self.assertEqual(svc._dampen("mira", "interest", 0.18, 0.28), (0.18, 0.28))


class TestPolicyMigration(unittest.TestCase):
    def test_old_store_gets_new_ttl(self):
        root = pathlib.Path(tempfile.mkdtemp())
        old = dict(DEFAULT_POLICY)
        old["affect_ttl_h"] = 48                     # 舊政策
        store = aem_v2.Store(root=str(root / "mira"), agent="app")
        store.init(old, 1792800000)
        svc = AffectService(root)
        svc.ensure("mira")
        state = store.show()
        self.assertEqual(state["policy"]["values"]["affect_ttl_h"],
                         DEFAULT_POLICY["affect_ttl_h"])
        self.assertEqual(state["policy"]["revision"], 2, "政策要 bump revision")
        svc.mood("mira")                          # 換政策後仍要能正常投影（view 有重算）

    def test_same_policy_is_left_alone(self):
        root = pathlib.Path(tempfile.mkdtemp())
        store = aem_v2.Store(root=str(root / "mira"), agent="app")
        store.init(DEFAULT_POLICY, 1792800000)
        svc = AffectService(root)
        svc.ensure("mira")
        self.assertEqual(store.show()["policy"]["revision"], 1, "沒有變更就不要動它")


class TestEventTable(unittest.TestCase):
    def test_event_table_has_a_downward_entry(self):
        self.assertTrue(any(dv < 0 for dv, _da in EVENT_AFFECT.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
