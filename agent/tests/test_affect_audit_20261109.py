#!/usr/bin/env python3
"""AEM 稽核（2026-11-09）的五組修正——每一條都對應報告裡的一個實測缺陷。

  ① 誰的話算數：客人／同伴的話不算「主人跟我說話」（不寫 message、不動缺席計時）。
  ② 尺標定：q0 10→3、標籤門檻按重放分布（0.07／−0.04／arousal 0.12）。
  ③ 信心：送進核心前乘一次（核心的 `con = 2·dv/conf` 會把信心約掉）。
  ④ 缺席重新武裝＋arousal 快核（峰值 402 秒 → ≈44 秒）。
  ⑤ 獨處時間以持久化的 `facts.last_user_message_at` 為準（重啟不忘）。
"""

from __future__ import annotations

import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.turns import TurnService
from agent.brain.domain.principal import Principal, PrincipalRegistry
from agent.brain.runtime.inner import affect_repair
from agent.brain.runtime.inner.affect import AffectService, _label
from agent.brain.vendor import aem_v2

T0 = 1_800_000_000.0


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [T0]
        self.affect = AffectService(self.tmp.name, clock=lambda: self.now[0])

    def say(self, text: str, **appraisal) -> str:
        """主人說一句 ⇒ 回那個 message 的 event_id（判定必須掛在這條上）。"""
        noted = self.affect.note_message("mira", text, speaker="owner")
        self.assertTrue(noted.get("ok"), noted)
        if appraisal:
            self.affect.appraise("mira", source_event_id=noted["event_id"], **appraisal)
        return str(noted["event_id"])

    def facts(self) -> dict:
        return self.affect._store("mira").show()["facts"]

    def receipts(self, kind: str = "message") -> list:
        return [row for row in self.affect._store("mira").show()["receipts"]
                if row["kind"] == kind]


class TestConfidenceScales(Base):
    def _valence_after(self, second_conf: float) -> float:
        self.say("ひとつめ", kind="interest", delta_v=0.4, delta_a=0.4, confidence=1.0)
        self.say("ふたつめ", kind="interest", delta_v=0.4, delta_a=0.4, confidence=second_conf)
        return self.affect.mood("mira", at=self.now[0])["valence"]

    def test_low_confidence_pushes_less(self) -> None:
        """核心會把信心約掉（兩筆 dv=0.4 的 1.0／0.3 原本都算全額）；乘過之後才有差。"""
        full = self._valence_after(1.0)
        self.setUp()
        low = self._valence_after(0.3)
        self.assertGreater(full, low + 0.05, f"低信心應該明顯輕：full={full} low={low}")
        self.assertAlmostEqual(low, 0.242, delta=0.02, msg="0.4×1.0 ＋ 0.4×0.3、q0=3 的期望值")


class TestWhoCounts(Base):
    def _service(self, affect=None) -> TurnService:
        principals = PrincipalRegistry(default="owner")
        principals.register(Principal("owner", "主人", owner=True), replace=True)
        principals.register(Principal("guest-x", "客人"), replace=True)
        return TurnService(runtime=None, threads=None, pending_store=None, lives={},
                           principals=principals, affect=affect or self.affect)

    def test_guest_talk_is_not_owner_talk(self) -> None:
        service = self._service()
        service._note_affect("mira", "こんにちは", "guest-x", source="app")
        facts = self.facts()
        self.assertIsNone(facts["last_user_message_at"], "客人的話不能算「主人來了」")
        self.assertEqual(facts["message_count"], 0)
        self.assertEqual(self.receipts("message"), [])
        rows = self.affect.timeline("mira")
        self.assertEqual(len(rows), 1, "仍要有一點情緒（走 outcome）")
        self.assertEqual(rows[0]["target"], "guest-x")

    def test_owner_talk_still_counts(self) -> None:
        service = self._service()
        service._note_affect("mira", "ただいま", "owner", source="app")
        self.assertEqual(self.facts()["last_user_message_at"], T0)
        self.assertEqual(len(self.receipts("message")), 1)

    def test_barge_in_does_not_fake_owner_return(self) -> None:
        """被打斷以前會偽造「（打斷）」message ⇒ 核心以 owner_returned 關掉缺席。"""
        self.affect.note_message("mira", "まだかな", speaker="owner")
        self.now[0] = T0 + 5500
        self.affect.advance("mira")
        self.assertIsNotNone(self.facts()["absence_episode_id"], "先把缺席點起來")
        self.now[0] = T0 + 5600
        self.affect.interrupted("mira")
        facts = self.facts()
        self.assertEqual(facts["last_user_message_at"], T0, "打斷不是主人說話")
        self.assertIsNotNone(facts["absence_episode_id"], "缺席不能被 owner_returned 關掉")
        self.assertEqual(len(self.receipts("message")), 1, "沒有第二條 message")

    def test_state_elicit_fallback_does_not_fake_message(self) -> None:
        self.affect.note_state("mira", {"emotion": {"label": "不安", "intensity": 0.6,
                                                       "valence": -0.5}},
                               source_event_id="no-such-message")
        self.assertEqual(self.receipts("message"), [], "狀態階段沒有 message 載體 ⇒ 走 outcome")
        rows = self.affect.timeline("mira")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "concern")


class TestScaleAndAbsence(Base):
    def test_label_bands_are_reachable(self) -> None:
        self.assertEqual(_label(0.10, 0.16), "わくわく")
        self.assertEqual(_label(0.10, 0.02), "ごきげん")
        self.assertEqual(_label(-0.06, 0.02), "しょんぼり")
        self.assertEqual(_label(0.03, 0.02), "ふつう")

    def test_single_exchange_moves_the_needle(self) -> None:
        """一輪對話要推得動（舊尺 q0=10 時只有 0.036，永遠到不了任何標籤）。"""
        self.say("おはよう", kind="interest", delta_v=0.18, delta_a=0.28, confidence=0.6,
                 dampen=False)
        valence = self.affect.mood("mira", at=self.now[0])["valence"]
        self.assertGreater(valence, 0.04, f"一輪對話要看得見（實測 {valence}）")

    def test_absence_rearms_after_its_ttl(self) -> None:
        self.affect.note_message("mira", "いってきます", speaker="owner")
        self.now[0] = T0 + 5500
        self.affect.advance("mira")
        first = self.facts()["absence_episode_id"]
        self.assertIsNotNone(first)
        self.now[0] = T0 + 5500 + 13 * 3600        # 舊集 ttl=12h 已過
        self.affect.advance("mira")
        second = self.facts()["absence_episode_id"]
        self.assertIsNotNone(second, "長假要能重新武裝，不然 12 小時後她就不再想他")
        episodes = {ep["id"]: ep for ep in self.affect._store("mira").show()["episodes"]}
        fresh = episodes.get(second) or {}
        self.assertGreater(float((fresh.get("affect") or {}).get("expires_at") or 0),
                           self.now[0], "重新武裝出來的缺席要還在有效期內")
        self.assertGreaterEqual(float(fresh.get("occurred_at") or 0), T0 + 5500,
                                "要是新的那一集，不是舊的殘留")

    def test_arousal_peaks_in_under_a_minute(self) -> None:
        """舊二階核峰值在 402 秒（回話那 5–20 秒只到 7.5%）⇒ 她「當下沒上頭、六分鐘後才上頭」。"""
        self.say("やった", kind="pleasure", delta_v=0.5, delta_a=0.8, confidence=1.0,
                 dampen=False)
        at = lambda seconds: self.affect.mood("mira", at=T0 + seconds)["arousal"]
        early, peak, late = at(10), at(45), at(600)
        self.assertGreater(early, 0.35, f"回話那一刻就該有感覺（10s={early}）")
        self.assertGreater(peak, early)
        self.assertGreater(peak, late)
        self.assertLess(late, 0.2, f"十分鐘後應該退掉（600s={late}）")


class TestPersistedClock(Base):
    def test_idle_survives_restart(self) -> None:
        """以前 idle 只看進程內 `_last_touch`：重啟歸零，跟核心的缺席計時打架。"""
        self.affect.note_message("mira", "まだ起きてる？", speaker="owner")
        restarted = AffectService(self.tmp.name, clock=lambda: T0 + 3 * 3600)
        self.assertAlmostEqual(restarted.idle_s("mira"), 3 * 3600, delta=5)

    def test_repair_helpers_are_exposed(self) -> None:
        for name in ("idle_s", "migrate_policy", "rearm_absence", "reproject", "scale_delta"):
            self.assertTrue(callable(getattr(affect_repair, name)), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
