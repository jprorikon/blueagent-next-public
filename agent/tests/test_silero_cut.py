#!/usr/bin/env python3
"""silero-vad 切句（現役同一顆模型）——用真音檔驗，不用合成音。

判據：
  ① 真人語音（我們自己的 TTS 檔）切成 1 句，且**含前置**（起音沒被吃掉）；
  ② 純靜音／純噪聲 切不出句子（能量法在這兩種上都會誤觸）；
  ③ 模型不在 ⇒ `SileroCutter` 明確報錯（上層會退回能量法，不會靜靜失效）。
"""

from __future__ import annotations

import io
import unittest
import wave
from pathlib import Path

try:
    import numpy as np
except ImportError:                     # 開發機沒裝 numpy（image 裡有，見 requirements.txt）⇒ 整支跳過，不算錯
    raise unittest.SkipTest("numpy 沒裝（image 裡有；本機開發可略過）")

from demo_common import *  # noqa: F401,F403,E402

from agent.adapter.silero_cut import DEFAULT_VAD_MODEL, SR, SileroCutter  # noqa: E402

WAV = Path("/tmp/ds_mira.wav")


def read_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as clip:
        rate, channels = clip.getframerate(), clip.getnchannels()
        frames = clip.readframes(clip.getnframes())
    if channels > 1:
        import audioop
        frames = audioop.tomono(frames, 2, 0.5, 0.5)
    if rate != SR:
        import audioop
        frames, _ = audioop.ratecv(frames, 2, 1, rate, SR, None)
    return np.frombuffer(frames, dtype=np.int16)


def blocks(x: np.ndarray, ms: int = 500):
    size = int(SR * ms / 1000)
    for index in range(0, len(x), size):
        yield x[index:index + size]


@unittest.skipUnless(DEFAULT_VAD_MODEL.is_file(), "silero 模型不在這台機器上")
class TestSileroCutter(unittest.TestCase):
    def test_speech_is_cut_and_keeps_the_onset(self):
        if not WAV.is_file():
            self.skipTest("沒有測試音檔 /tmp/ds_mira.wav")
        audio = read_16k(WAV)
        cutter = SileroCutter()
        found = []
        for block in blocks(audio):
            found += cutter.feed(block)
        found += cutter.flush()
        self.assertGreaterEqual(len(found), 1, "真人語音至少要切出一段")
        # 前置（preroll）確實有補：同一段音，preroll 500 ms 切出來的段要**比 preroll 0 更長**
        without = SileroCutter(preroll_ms=0)
        base = []
        for block in blocks(audio):
            base += without.feed(block)
        base += without.flush()
        self.assertTrue(base)
        self.assertGreater(len(found[0]), len(base[0]),
                           "preroll 沒補上（兩者一樣長就代表起音會被吃掉）")

    def test_quiet_noise_makes_no_sentence(self):
        cutter = SileroCutter()
        noise = (np.random.default_rng(0).normal(0, 60, SR * 4)).astype(np.int16)
        found = []
        for block in blocks(noise):
            found += cutter.feed(block)
        found += cutter.flush()
        self.assertEqual(found, [], "低電平噪聲不該被當成一句話")

    def test_missing_model_is_loud(self):
        with self.assertRaises(FileNotFoundError):
            SileroCutter(model="/nonexistent/silero.onnx")


class TestVoiceSessionPrefersSilero(unittest.TestCase):
    def test_session_uses_silero_when_available(self):
        from agent.adapter.discord_voice import VoiceConfig, DiscordVoiceSession

        class _State:
            demo = type("D", (), {"stt": None})()

        source = type("S", (), {"config": type("C", (), {"owner_ids": ()})(),
                                "state": _State()})()
        session = DiscordVoiceSession(VoiceConfig(enabled=True, channel_id="vc"), source)
        if not DEFAULT_VAD_MODEL.is_file():
            self.assertIsNone(session.cutter)
            self.assertIsNotNone(session.buffer, "沒有 silero 時要退回能量切句")
        else:
            self.assertIsNotNone(session.cutter, "有 silero 就該用 silero")


if __name__ == "__main__":
    unittest.main(verbosity=2)
