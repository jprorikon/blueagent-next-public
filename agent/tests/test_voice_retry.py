#!/usr/bin/env python3
"""「文字有、語音只有半句」：一句合成失敗不能吃掉後面全部。

owner 2026-10-22 實測：`turns.jsonl` 那一輪 `speech: 2, speech_errors: 1`
⇒ 第三句沒聲音，聽起來就是「只講了半句」。

修法（三條）：
  ① 單句失敗**立刻重試一次**（橋忙／逾時是偶發的）；
  ② 冷卻從 60 s 降到 8 s（不然重試直接吃 `tts_down`）；
  ③ 連續 3 句都失敗才收手 —— 一句壞不該讓整輪變成默劇；
  ④ 失敗原因寫進 `turns.jsonl`（`speech_error_reasons`），查得出來是誰的錯。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.voice import VoiceService, _StreamSpeaker


class FakeTts:
    """前 `fail_times` 次失敗，之後成功。"""

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def speak(self, text: str, voice: str):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("bridge 500")

        class Clip:
            def payload(self):
                return {"audio": "AAAA", "seconds": 0.5}

        return [Clip()]


class FakeEntities:
    def get(self, entity):
        class Entity:
            id = entity
            voice = ""

        return Entity()


def speaker_with(tts, *, log=None) -> _StreamSpeaker:
    voice = VoiceService(FakeEntities(), log=log or (lambda _m: None))
    voice.tts = tts
    speaker = _StreamSpeaker(voice, "mira", thread_id="t1", principal="owner",
                             force=True, target="discord:vc", source="discord:vc")
    speaker._prepare = lambda: None            # 跳過發言權/房間判定
    speaker._started = True
    speaker._enabled = True
    speaker._call_target = "discord:vc"
    return speaker


class TestRetryAndContinue(unittest.TestCase):
    def test_single_failure_is_retried(self):
        tts = FakeTts(fail_times=1)
        speaker = speaker_with(tts)
        frames = speaker.speak("一句目です。")
        self.assertTrue(frames, "失敗一次要重試，重試成功就有聲音")
        self.assertEqual(tts.calls, 2, f"應該剛好重試一次：{tts.calls}")
        self.assertEqual(speaker._failed, 0)

    def test_one_bad_sentence_does_not_silence_the_rest(self):
        tts = FakeTts(fail_times=99)           # 一直壞
        speaker = speaker_with(tts)
        self.assertEqual(speaker.speak("一句目。"), [])
        self.assertEqual(speaker._failed, 1)
        self.assertTrue(speaker._enabled, "才失敗一句，不該整輪關掉嘴")
        tts.fail_times = 0                     # 橋恢復
        tts.calls = 0
        frames = speaker.speak("二句目。")
        self.assertTrue(frames, "後面的句子要繼續試（不然又是『只講半句』）")

    def test_three_consecutive_failures_stop_trying(self):
        speaker = speaker_with(FakeTts(fail_times=99))
        for _ in range(3):
            speaker.speak("だめ。")
        self.assertFalse(speaker._enabled, "連續三句都壞才收手（避免每句都等 timeout）")

    def test_failure_reason_is_kept(self):
        speaker = speaker_with(FakeTts(fail_times=99))
        speaker.speak("だめ。")
        self.assertIn("tts_failed", speaker.last_error)

    def test_reason_is_logged(self):
        logs: list[str] = []
        speaker = speaker_with(FakeTts(fail_times=99), log=logs.append)
        speaker.speak("だめ。")
        self.assertTrue(any("合成失敗" in line for line in logs), logs)


class TestTelemetryKeepsReason(unittest.TestCase):
    def test_turn_row_records_reasons(self):
        state = make_state()
        self.addCleanup(state.shutdown)
        state.send("今日はどう？", principal="owner")
        row = state.app.telemetry.tail(1)
        self.assertTrue(row, "應該有紀錄")
        self.assertIn("speech_error_reasons", row[0], f"要留原因：{row[0]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
