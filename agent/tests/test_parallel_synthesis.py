#!/usr/bin/env python3
"""多句並行合成（照舊橋 `ba/voice/frames.py` 的做法，owner 2026-10-24）。

舊橋的做法（2026-10-04 定案）：整段文字拿到 → 切句 → **一次投遞到執行緒池** →
依 index 順序取結果、合成好一句立刻發一句（第一句 priority=0 先拿實例）。
總牆鐘從「句數×單句」變成「≈最慢那句」，且**送出順序不變**（跳號會被端末當殘幀丟掉）。

next 這輪的用法：串流時只合成第一句（首句優先），其餘排隊；模型吐完再一次
`speak_many()` 並行補完。這支守三件事：
  ① 三句 0.3 s、兩個工作 ⇒ 總耗時 < 0.6 s（逐句會是 0.9 s）
  ② frames 照句子順序回來
  ③ 打斷之後不再吐後面的句子；`spoken` 只記真的合成成功的
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.domain.entity import EntityRegistry  # noqa: E402
from agent.brain.runtime.voice import VoiceService  # noqa: E402


class SlowTts:
    """真的會睡的假 TTS（`_speak_real` 會呼叫 `clip.payload()`）。"""

    def __init__(self, seconds: float = 0.3) -> None:
        self.seconds = seconds
        self.seen: list[str] = []
        self._lock = threading.Lock()
        self._peak = 0
        self._live = 0

    @property
    def peak(self) -> int:
        return self._peak

    def speak(self, text: str, voice: str):
        with self._lock:
            self.seen.append(text)
            self._live += 1
            self._peak = max(self._peak, self._live)
        time.sleep(self.seconds)
        with self._lock:
            self._live -= 1

        class Clip:
            def payload(self_inner):
                return {"text": text, "audio": f"wav-{text}", "seconds": 1.0}

        return [Clip()]


def build(seconds: float = 0.3):
    tts = SlowTts(seconds)
    voice = VoiceService(EntityRegistry(), tts=tts)
    speaker = voice.begin_stream("mira", thread_id="th-1", principal="owner",
                                 force=True, target="phone:local", source="app")
    return speaker, tts


class TestSpeakMany(unittest.TestCase):
    def test_overlaps_and_keeps_order(self):
        speaker, tts = build(0.3)
        started = time.monotonic()
        frames = speaker.speak_many(["一句目。", "二句目。", "三句目。", "四句目。"])
        spent = time.monotonic() - started
        self.assertEqual([frame["payload"]["text"] for frame in frames],
                         ["一句目。", "二句目。", "三句目。", "四句目。"], "順序不能亂")
        # 四句 × 0.3 s：逐句要 1.2 s，兩路併發只要兩輪 ≈ 0.6 s
        self.assertLess(spent, 0.9, f"四句應並行跑完（逐句會是 1.2 s），實際 {spent:.2f}s")
        self.assertGreaterEqual(tts.peak, 2, "應該真的有兩句同時在合成")
        self.assertEqual(speaker.spoken, "一句目。二句目。三句目。四句目。")

    def test_cancel_skips_remaining_sentences(self):
        speaker, _ = build(0.2)
        speaker.speak("一句目。")            # 首句（串流時合成）
        speaker.cancel()
        frames = speaker.speak_many(["二句目。", "三句目。"])
        self.assertEqual(frames, [], "被打斷之後不該再吐聲音")

    def test_single_sentence_falls_back_to_sync(self):
        speaker, _ = build(0.02)
        frames = speaker.speak_many(["だけ。"])
        self.assertEqual([frame["payload"]["text"] for frame in frames], ["だけ。"])

    def test_empty_input_is_noop(self):
        speaker, tts = build(0.01)
        self.assertEqual(speaker.speak_many([]), [])
        self.assertEqual(tts.seen, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
