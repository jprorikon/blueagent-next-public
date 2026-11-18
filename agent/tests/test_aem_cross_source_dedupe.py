#!/usr/bin/env python3
"""跨源同句合併：**30 秒內、非同源、同一句話 ⇒ 算一件**（owner 2026-11-01 定）。

為什麼要有這條：AEM 的 message receipt 是靠 `event_id` 去重的（`aem_v2.apply_event`），
而我們 `note_message` 預設拿「時鐘＋文本」算 id ⇒ 同一句話從手機與 Discord 各進來一次，
就是兩條 receipt、兩次 appraisal、valence／arousal 各加一次。
強度翻倍只是表面；真正問題是**入口分布會把尺帶歪**——手機常斷、Discord 常開，
偏移來自「用哪個端」而不是「他說了什麼」。

判據（本檔釘住的）：
  1. 雙源同句 3 秒內 ⇒ 一條 message、一次判定（含 `duplicate` 不當錯誤）
  2. 同源同句 3 秒內 ⇒ **兩條**（他真有可能講兩次）
  3. 雙源同句但隔 35 秒 ⇒ 兩條（窗口是 30 秒）
  4. 雙源不同句 ⇒ 兩條
  5. 全形空白／多空白 ⇒ 仍算同一句（NFKC＋空白收斂）
  6. 重啟（新 service、同一個 root）⇒ 窗內仍併（索引有落盤）
  7. `BLUEAGENT_AEM_DEDUPE_S=0` ⇒ 回舊行為（兩條）
"""

from __future__ import annotations

import os
import tempfile
import unittest

from agent.brain.runtime.inner.affect import AffectService


class Clock:
    """可推進的假時鐘（AEM 全部時間都吃這個）。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += float(seconds)


def _messages(service: AffectService, entity: str = "mira") -> list[dict]:
    state = service._store(entity).show()
    return [row for row in (state.get("receipts") or []) if row.get("kind") == "message"]


def _episodes(service: AffectService, entity: str = "mira") -> list[dict]:
    return list(service._store(entity).show().get("episodes") or [])


class TestCrossSourceDedupe(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ba-dedupe-")
        self.clock = Clock()
        self.service = AffectService(self.root, enabled=True, clock=self.clock)
        self.service.ensure("mira")
        self.logs: list[str] = []
        self.service._log = self.logs.append

    def _say(self, text: str, source: str, *, appraise: bool = False) -> dict:
        noted = self.service.note_message("mira", text, speaker="owner", source=source)
        if appraise and noted.get("ok"):
            noted["appraise"] = self.service.appraise(
                "mira", kind="interest", delta_v=0.18, delta_a=0.28,
                confidence=0.6, source_event_id=str(noted["event_id"]))
        return noted

    # 1 ── 雙源同句 ⇒ 一件
    def test_two_sources_same_sentence_is_one(self) -> None:
        first = self._say("こんばんは", "app", appraise=True)
        self.clock.tick(3)
        second = self._say("こんばんは", "discord:dm", appraise=True)
        self.assertTrue(second.get("deduped"), "第二個入口要標成併掉的")
        self.assertEqual(second["event_id"], first["event_id"], "要沿用第一條的 event_id")
        self.assertEqual(second.get("deduped_from"), "app")
        self.assertEqual(len(_messages(self.service)), 1, "只該有一條 message receipt")
        facts = self.service._store("mira").show()["facts"]
        self.assertEqual(facts["message_count"], 1, "message_count 不能加兩次")
        # 第二個入口的 appraisal 拿的是同一條 source ⇒ 核心回 JUDGE_ALREADY_FINAL，
        # 我們的適配層要把它當「規則生效」而不是錯誤。
        self.assertTrue(second["appraise"].get("ok"))
        self.assertTrue(second["appraise"].get("duplicate"))
        self.assertEqual(len(_episodes(self.service)), 1, "情緒只該被加一次")
        self.assertTrue(any("跨源同句併為一件" in line for line in self.logs))

    # 2 ── 同源同句 ⇒ 兩件
    def test_same_source_repeat_is_two(self) -> None:
        self._say("もう一回言うね", "app")
        self.clock.tick(3)
        again = self._say("もう一回言うね", "app")
        self.assertFalse(again.get("deduped"), "同源不算跨源，不能併")
        self.assertEqual(len(_messages(self.service)), 2)

    # 3 ── 超過窗口 ⇒ 兩件
    def test_window_expiry(self) -> None:
        self._say("おはよう", "app")
        self.clock.tick(35)
        later = self._say("おはよう", "discord:dm")
        self.assertFalse(later.get("deduped"))
        self.assertEqual(len(_messages(self.service)), 2)

    # 4 ── 不同句 ⇒ 兩件
    def test_different_sentence_is_two(self) -> None:
        self._say("おはよう", "app")
        self.clock.tick(2)
        other = self._say("おやすみ", "discord:dm")
        self.assertFalse(other.get("deduped"))
        self.assertEqual(len(_messages(self.service)), 2)

    # 5 ── 全形空白／多空白 ⇒ 仍算同一句
    def test_normalization(self) -> None:
        self._say("今日は　疲れた", "app")
        self.clock.tick(5)
        merged = self._say("  今日は 疲れた ", "discord:dm")
        self.assertTrue(merged.get("deduped"), "NFKC＋空白收斂之後應視為同一句")
        self.assertEqual(len(_messages(self.service)), 1)

    # 6 ── 重啟（新 service、同一個 root）⇒ 窗內仍併
    def test_restart_keeps_window(self) -> None:
        self._say("ただいま", "app")
        self.clock.tick(10)
        restarted = AffectService(self.root, enabled=True, clock=self.clock)
        restarted._log = self.logs.append
        merged = restarted.note_message("mira", "ただいま", speaker="owner",
                                        source="discord:dm")
        self.assertTrue(merged.get("deduped"), "索引有落盤，重啟不該漏併")
        self.assertEqual(len(_messages(restarted)), 1)

    # 7 ── 關掉這條規則 ⇒ 回舊行為
    def test_disabled_by_env(self) -> None:
        os.environ["BLUEAGENT_AEM_DEDUPE_S"] = "0"
        try:
            service = AffectService(self.root + "-off", enabled=True, clock=self.clock)
            service.ensure("mira")
            service.note_message("mira", "やあ", speaker="owner", source="app")
            self.clock.tick(1)
            second = service.note_message("mira", "やあ", speaker="owner",
                                         source="discord:dm")
            self.assertFalse(second.get("deduped"))
            self.assertEqual(len(_messages(service)), 2, "關掉之後就是舊行為：兩條")
        finally:
            os.environ.pop("BLUEAGENT_AEM_DEDUPE_S", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
