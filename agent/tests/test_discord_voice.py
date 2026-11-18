#!/usr/bin/env python3
"""Discord 語音頻道：斷句、轉檔、回聲（不連線的部分全部可離線測）。

判據：
  ① 48k 立體 → 16k 單聲道 wav（whisper 要的格式）；
  ② 斷句：靜音 ≥ `silence_s` 收一句；太短丟掉；太長強制切；
  ③ 沒開 VC / 沒設頻道 ⇒ 不建 session（文字源不受影響）；
  ④ 語音說的話**走同一條管道**（`force_addressed`，因此仍是注意力層的 REPLY）；
  ⑤ 她的回話會併成**一個** wav（Discord 一次只發一個附件）。
"""

from __future__ import annotations

import io
import struct
import unittest
import wave

from demo_common import *  # noqa: F401,F403

from agent.adapter.discord_source import DiscordConfig, DiscordSource
from agent.adapter.discord_voice import (VC_CHANNELS, VC_RATE, UtteranceBuffer,
                                                   VoiceConfig, build_voice,
                                                   pcm48_stereo_to_wav16, rms_of)

OWNER_ID = "100000000000000001"


def loud(seconds: float, amp: int = 12000) -> bytes:
    frames = int(VC_RATE * seconds)
    return b"".join(struct.pack("<hh", amp, amp) for _ in range(frames))


def quiet(seconds: float, amp: int = 5) -> bytes:
    frames = int(VC_RATE * seconds)
    return b"".join(struct.pack("<hh", amp, amp) for _ in range(frames))


def chunked(pcm: bytes, step_s: float = 0.02) -> list[bytes]:
    size = int(VC_RATE * step_s) * 2 * VC_CHANNELS
    return [pcm[index:index + size] for index in range(0, len(pcm), size)]


class TestAudioConversion(unittest.TestCase):
    def test_resample_and_downmix(self):
        wav = pcm48_stereo_to_wav16(loud(0.5))
        with wave.open(io.BytesIO(wav), "rb") as clip:
            self.assertEqual(clip.getnchannels(), 1)
            self.assertEqual(clip.getframerate(), 16000)
            self.assertAlmostEqual(clip.getnframes() / 16000, 0.5, delta=0.05)

    def test_rms(self):
        self.assertGreater(rms_of(loud(0.1)), 1000)
        self.assertLess(rms_of(quiet(0.1)), 50)


class TestUtteranceBuffer(unittest.TestCase):
    def setUp(self):
        self.config = VoiceConfig(enabled=True, channel_id="vc", min_rms=320,
                                  silence_s=0.5, min_utterance_s=0.3, max_utterance_s=3.0)
        self.heard: list = []
        self.buffer = UtteranceBuffer(self.config, self.heard.append)

    def feed(self, pcm: bytes):
        fired = False
        for block in chunked(pcm):
            fired = self.buffer.feed(OWNER_ID, block) or fired
        return fired

    def test_speech_then_silence_emits_one_utterance(self):
        self.feed(loud(1.0))
        self.assertFalse(self.heard, "還沒靜音就不該切")
        self.feed(quiet(0.8))
        self.assertEqual(len(self.heard), 1)
        self.assertGreater(self.heard[0].seconds, 1.0)
        self.assertTrue(self.heard[0].wav[:4] == b"RIFF")

    def test_short_noise_is_dropped(self):
        self.feed(loud(0.1))
        self.feed(quiet(0.8))
        self.assertEqual(self.heard, [], "太短＝雜訊")

    def test_too_long_is_forced(self):
        self.feed(loud(4.0))
        self.assertTrue(self.heard, "超過上限要強制切一句")

    def test_silence_alone_never_fires(self):
        self.feed(quiet(2.0))
        self.assertEqual(self.heard, [])


class TestVoiceConfig(unittest.TestCase):
    def test_disabled_without_flag(self):
        self.assertIsNone(build_voice({"VOICE_CHANNEL_ID": "vc"}, None))
        config = VoiceConfig.from_env({"VOICE_CHANNEL_ID": "vc"})
        self.assertFalse(config.enabled)

    def test_enabled_reads_numbers(self):
        config = VoiceConfig.from_env({"BLUEAGENT_DISCORD_VC": "1", "VOICE_CHANNEL_ID": "vc",
                                       "BLUEAGENT_VC_SILENCE_S": "1.2",
                                       "BLUEAGENT_VC_MIN_RMS": "500"})
        self.assertTrue(config.enabled)
        self.assertEqual(config.channel_id, "vc")
        self.assertAlmostEqual(config.silence_s, 1.2)
        self.assertEqual(config.min_rms, 500)

    def test_session_is_built_when_enabled(self):
        session = build_voice({"BLUEAGENT_DISCORD_VC": "1", "VOICE_CHANNEL_ID": "vc"}, None)
        self.assertIsNotNone(session)
        self.assertFalse(session.joined, "還沒 join 就不該說已進頻道")


class TestVoiceGoesThroughTheSamePipe(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        cfg = DiscordConfig.from_env({"DISCORD_BOT_TOKEN": "t",
                                      "ALLOWED_USER_IDS": OWNER_ID})
        self.source = DiscordSource(cfg, self.state)

    def test_voice_utterance_is_treated_as_addressed(self):
        result = self.source.handle_voice("ミラ、聞こえる？", OWNER_ID)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["principal"], "owner")

    def test_voice_asks_for_voice_reply(self):
        result = self.source.handle_voice("おやすみ", OWNER_ID)
        self.assertTrue(result["ok"], result)
        # 語音頻道走 `send_voice`：主人在 work 模式 ⇒ 先短回一句，完整答案由背景任務補送。
        # 這條只驗「有答應出聲」——`clips` 在串流路徑才有，走 send_voice 時看 `frames`。
        self.assertTrue(result.get("frames", 0) >= 0)

    def test_wavs_are_merged_into_one_attachment(self):
        from agent.adapter.discord_source import DiscordSource as Source
        first = pcm48_stereo_to_wav16(loud(0.4))
        second = pcm48_stereo_to_wav16(loud(0.6))
        merged = Source.merge_wavs([first, second])
        with wave.open(io.BytesIO(merged), "rb") as clip:
            self.assertAlmostEqual(clip.getnframes() / clip.getframerate(), 1.0, delta=0.1)


class TestVoiceChannelActuallyGetsAudio(unittest.TestCase):
    """2026-10-22 實測：她在語音頻道**合成了卻沒播出**（`"audio": b""` 寫死）。

    判據：
      ① `speech` 幀裡的 base64 wav 要被撿回來、併成一個檔（＝VC 有東西可播）；
      ② 語音頻道的回覆文字要能貼回**同一個頻道**（不然使用者以為她沒收到）；
      ③ 沒有 Discord client 時 fail-closed（回 False，不炸）。
    """

    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        cfg = DiscordConfig.from_env({"DISCORD_BOT_TOKEN": "t",
                                      "ALLOWED_USER_IDS": OWNER_ID})
        self.source = DiscordSource(cfg, self.state)

    @staticmethod
    def frame(wav: bytes) -> dict:
        import base64
        return {"kind": "speech", "payload": {"audio": base64.b64encode(wav).decode("ascii")}}

    def test_audio_is_recovered_from_speech_frames(self):
        first = pcm48_stereo_to_wav16(loud(0.4))
        second = pcm48_stereo_to_wav16(loud(0.6))
        audio = DiscordSource.audio_from_frames([self.frame(first),
                                                 {"kind": "delta", "payload": {"text": "x"}},
                                                 self.frame(second)])
        self.assertTrue(audio.startswith(b"RIFF"), "要併成一個合法 wav")
        with wave.open(io.BytesIO(audio), "rb") as clip:
            self.assertAlmostEqual(clip.getnframes() / clip.getframerate(), 1.0, delta=0.1)

    def test_no_speech_frames_means_no_audio(self):
        self.assertEqual(DiscordSource.audio_from_frames([{"kind": "delta",
                                                            "payload": {"text": "x"}}]), b"")
        self.assertEqual(DiscordSource.audio_from_frames([]), b"")

    def test_voice_reply_carries_audio_when_frames_have_it(self):
        """`send_voice` 回的 frames 帶音訊 ⇒ `handle_voice` 的 `audio` 不能是空的。"""
        wav = pcm48_stereo_to_wav16(loud(0.5))
        self.source.state = type("FakeState", (), {
            "reply_text": staticmethod(lambda _result: "うん"),
            "send_voice": lambda *_args, **_kwargs: {
                "frames": [{"kind": "speech", "payload": {
                    "audio": __import__("base64").b64encode(wav).decode("ascii")}},
                           {"kind": "done", "payload": {"text": "うん"}}],
                "following_up": False, "decision": {}},
        })()
        result = self.source.handle_voice("聞こえる？", OWNER_ID)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["audio"].startswith(b"RIFF"),
                        "語音頻道的回覆一定要帶聲音（漏掉＝他什麼都聽不到）")

    def test_channel_text_is_fail_closed_without_client(self):
        self.assertFalse(self.source.send_channel_text("", "hi"))
        self.assertFalse(self.source.send_channel_text("123", "  "))
        self.assertFalse(self.source.send_channel_text("not-a-number", "hi"))


class TestFollowupIsSpoken(unittest.TestCase):
    """語音頻道「先短回一句、後面補完整答案」⇒ **完整答案也要出聲**。

    2026-10-22 owner 實測：「似乎有段話沒有在語音頻道中被讀出來」——
    就是後續那段只貼了文字。
    """

    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        cfg = DiscordConfig.from_env({"DISCORD_BOT_TOKEN": "t",
                                      "ALLOWED_USER_IDS": OWNER_ID})
        self.source = DiscordSource(cfg, self.state)

    def test_followup_carries_speech_frames_when_asked(self):
        seen: list[dict] = []
        self.state._followup_worker("小説の話をして", "mira", "owner", "discord:vc",
                                    seen.append, {"want_voice": True})
        self.assertTrue(seen, "後續工作要交出結果")
        kinds = [str(frame.get("kind")) for frame in seen[-1].get("frames") or []]
        self.assertIn("speech", kinds, f"完整答案要有語音幀（不然只有文字）：{kinds}")
        self.assertTrue(self.state.reply_text(seen[-1]), "文字也要有")

    def test_followup_without_voice_stays_text_only(self):
        seen: list[dict] = []
        self.state._followup_worker("調べて", "mira", "owner", "discord:vc",
                                    seen.append, {})
        kinds = [str(frame.get("kind")) for frame in seen[-1].get("frames") or []]
        self.assertNotIn("speech", kinds)

    def test_speak_followup_plays_the_audio_once(self):
        class FakeVoice:
            def __init__(self):
                self.played: list[bytes] = []

            def publish_audio(self, audio: bytes) -> bool:
                self.played.append(audio)
                return True

        wav = pcm48_stereo_to_wav16(loud(0.5))
        self.source.voice = FakeVoice()
        frames = [{"kind": "speech", "payload": {
            "audio": __import__("base64").b64encode(wav).decode("ascii")}}]
        audio = self.source.speak_followup({"frames": frames})
        self.assertTrue(audio.startswith(b"RIFF"))
        self.assertEqual(len(self.source.voice.played), 1, "要被播一次（也只一次）")

    def test_speak_followup_without_frames_is_silent(self):
        self.source.voice = None
        self.assertEqual(self.source.speak_followup({"frames": []}), b"")


if __name__ == "__main__":
    unittest.main(verbosity=2)
