#!/usr/bin/env python3
"""心情定位（mood line）的 `read` 段：**兩段分離 + 三條快取條件**（owner 2026-11-01 第①項）。

`label` 是詞表翻的短標（給程式比對）；`read` 是一次 LLM 寫的一句人話（給人與別的 session 讀）。
aster 那裡的實測：30 分沿用會變成「五拍同一句話」⇒ 這版預設 **240 秒**，而且三條一起成立才沿用：

  ① |Δv| < 0.010 且 |Δa| < 0.020
  ② 距上次 read 未超過 `age_s`
  ③ 沒有比這句更新的意見（`note_feedback()` ⇒ 一定重讀）

判據（本檔釘住的）：
  1. 第一次一定呼叫，並落盤
  2. 同樣的 v／a、240 秒內 ⇒ **不再呼叫**（回同一句）
  3. v（或 a）動超過門檻 ⇒ 重讀
  4. 超過 240 秒 ⇒ 重讀
  5. `note_feedback()` 之後 ⇒ 即使 v／a 沒動也重讀
  6. 呼叫失敗／回空 ⇒ **舊的那句留著**，不寫壞值
  7. 輸出洗乾淨（去引號／換行、長度上限）
  8. `BLUEAGENT_MOODLINE=0`（預設）⇒ 一次都不呼叫
  9. 心跳每跳都會叫它，但只有 stale 時才真的花呼叫
"""

from __future__ import annotations

import os
import tempfile
import unittest

from agent.brain.runtime.heartbeat import Heartbeat
from agent.brain.runtime.inner.moodline import MAX_CHARS, MoodLine


class Clock:
    def __init__(self, start: float = 10_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeCall:
    """假模型：記次數、可指定回傳或拋錯。"""

    def __init__(self, text: str = "ちょっとだけ、さみしい感じ。") -> None:
        self.text = text
        self.calls: list[str] = []
        self.error: Exception | None = None

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.error is not None:
            raise self.error
        return self.text


def mood_of(valence: float = 0.0, arousal: float = 0.2, label: str = "ふつう") -> dict:
    return {"entity": "mira", "valence": valence, "arousal": arousal,
            "label": label, "social_need": 0.0, "propensity": 0.1, "idle_s": 0.0}


class TestMoodLineRead(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ba-moodline-")
        self.clock = Clock()
        self.call = FakeCall()
        self.logs: list[str] = []
        self.line = MoodLine(self.root, call=self.call, clock=self.clock, enabled=True,
                             age_s=240.0, log=self.logs.append)

    def _read(self, mood: dict | None = None, **kwargs):
        return self.line.maybe_read("mira", mood if mood is not None else mood_of(), **kwargs)

    # 1 ── 第一次一定呼叫，並落盤
    def test_first_read_calls_and_persists(self) -> None:
        row = self._read()
        self.assertEqual(len(self.call.calls), 1)
        self.assertEqual(row["read"], self.call.text)
        self.assertEqual(row["v"], 0.0)
        self.assertTrue((self.line._path("mira")).exists(), "要落盤（重啟後不用重燒一次）")
        self.assertEqual(self.line.reads, 1)

    # 2 ── 沒動就沿用
    def test_unchanged_reuses(self) -> None:
        first = self._read()
        self.clock.tick(30)
        again = self._read()
        self.assertEqual(len(self.call.calls), 1, "Δv/Δa 都在門檻內、又未滿 240 秒 ⇒ 不該再花一次")
        self.assertEqual(again["read"], first["read"])
        self.assertEqual(self.line.reused, 1)

    # 3 ── 動超過門檻就重讀
    def test_valence_jump_rereads(self) -> None:
        self._read()
        self.clock.tick(30)
        self.call.text = "なんだか、ちょっといいことがありそう。"
        row = self._read(mood_of(valence=0.02))
        self.assertEqual(len(self.call.calls), 2, "Δv ≥ 0.010 ⇒ 重讀")
        self.assertIn("いいこと", row["read"])

    def test_arousal_jump_rereads(self) -> None:
        self._read()
        self.clock.tick(30)
        self._read(mood_of(arousal=0.25))
        self.assertEqual(len(self.call.calls), 2, "Δa ≥ 0.020 ⇒ 重讀")

    # 4 ── 過期就重讀
    def test_age_expiry_rereads(self) -> None:
        self._read()
        self.clock.tick(241)
        self._read()
        self.assertEqual(len(self.call.calls), 2, "超過 240 秒 ⇒ 重讀（不能五拍同一句）")

    # 5 ── 有新的意見 ⇒ 一定重讀
    def test_feedback_forces_reread(self) -> None:
        self._read()
        self.clock.tick(5)
        self.line.note_feedback("mira", "そこまで落ち込んでない")
        self._read()
        self.assertEqual(len(self.call.calls), 2, "被說寫錯了就要重讀，即使 v／a 沒動")

    # 6 ── 失敗／空字留舊的
    def test_failure_keeps_old(self) -> None:
        first = self._read()
        self.clock.tick(300)
        self.call.error = RuntimeError("boom")
        kept = self._read()
        self.assertEqual(kept["read"], first["read"], "呼叫失敗 ⇒ 舊的那句留著")
        self.assertEqual(self.line.failed, 1)
        self.call.error = None
        self.call.text = "   "                        # 回空也要留舊的
        blank = self._read()
        self.assertEqual(blank["read"], first["read"])
        self.assertEqual(self.line.failed, 2)

    # 7 ── 輸出洗乾淨
    def test_output_is_cleaned(self) -> None:
        self.call.text = '「ちょっと眠いかな。\n余計な行」'
        row = self._read()
        self.assertEqual(row["read"], "ちょっと眠いかな。")
        self.call.text = "あ" * (MAX_CHARS + 40)
        self.clock.tick(300)
        row = self._read()
        self.assertEqual(len(row["read"]), MAX_CHARS)

    # 8 ── 預設關（不在 compose 開就不花錢）
    def test_disabled_by_default(self) -> None:
        env = os.environ.pop("BLUEAGENT_MOODLINE", None)
        try:
            off = MoodLine(self.root + "-off", call=self.call, clock=self.clock)
            self.assertFalse(off.enabled, "程式預設關；要開是 compose 的事")
            self.assertEqual(off.maybe_read("mira", mood_of()), {})
            self.assertEqual(len(self.call.calls), 0)
        finally:
            if env is not None:
                os.environ["BLUEAGENT_MOODLINE"] = env

    # 9 ── 心跳每跳都會叫，但只有 stale 才花呼叫
    def test_heartbeat_hook_only_burns_when_stale(self) -> None:
        class Affect:
            def advance(self, entity):
                return {}

            def snapshot(self, entity):
                return {"arousal": 0.2}

            def mood(self, entity, at=None):
                return mood_of()

            def timeline(self, entity, limit=20):
                return [{"at": 1.0, "kind": "interest", "target": "owner",
                         "delta_valence": 0.18, "delta_arousal": 0.28, "expires_at": 0.0}]

        beat = Heartbeat(entity="mira", affect=Affect(), gate=None, moodline=self.line,
                         threshold=0.35, clock=self.clock)
        for _ in range(3):
            beat.tick()
            self.clock.tick(20)
        self.assertEqual(len(self.call.calls), 1, "三跳、40 秒內 ⇒ 只該真的呼叫一次")
        self.assertIn("interest", self.call.calls[0], "要帶上「為什麼」（最近推動數值的 episode）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
