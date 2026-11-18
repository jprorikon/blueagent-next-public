#!/usr/bin/env python3
"""打斷（barge-in）：主人一開口，她的聲音就停（交接檔 §6.1）。

判據：
  ① 開口那一刻有回呼（silero 上升沿／能量法上升沿都算），且一句只叫一次；
  ② 她在播聲音時主人開口 ⇒ `VoiceClient.stop()` 被叫、`playing` 轉 False、`interruptions` +1、日誌有「打斷」；
  ③ 白名單外的人／她本來沒在說話／`BLUEAGENT_VC_BARGE_IN=0` ⇒ 不打斷；
  ④ 打斷之後，**那一輪**的 `_StreamSpeaker` 再也吐不出 `speech` 幀（半句話不補播），
     且發言權被收回；
  ⑤ 打斷後的新一輪照常出聲（不是「一次打斷就永久啞掉」）。
"""

from __future__ import annotations

import unittest
import wave
import io

from demo_common import *  # noqa: F401,F403

from agent.adapter.discord_voice import (DiscordVoiceSession, UtteranceBuffer,
                                                   VoiceConfig)
from agent.brain.compose.entities import build_entities
from agent.brain.runtime.voice.arbiter import FloorArbiter
from agent.brain.runtime.voice import VoiceService

OWNER_ID = "100000000000000001"
OTHER_ID = "111111111111111111"


def tone_wav(seconds: float = 0.2, rate: int = 32000) -> bytes:
    """一段合法的 wav（真的 ffmpeg 也吃得下）。"""
    import struct
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        frames = int(rate * seconds)
        clip.writeframes(b"".join(struct.pack("<h", 1000 if i % 8 < 4 else -1000)
                                  for i in range(frames)))
    return buffer.getvalue()


def loud_block(seconds: float = 0.02, amp: int = 12000) -> bytes:
    import struct
    frames = int(48000 * seconds)
    return b"".join(struct.pack("<hh", amp, amp) for _ in range(frames))


class FakeVoiceClient:
    """只記「有沒有被叫 stop／play」，不碰網路。"""

    def __init__(self) -> None:
        self.stops = 0
        self.plays: list = []
        self.after = None

    def stop(self):
        self.stops += 1

    def play(self, source, *, after=None):
        self.plays.append(source)
        self.after = after

    def is_connected(self) -> bool:
        return True


class SessionFixture(unittest.TestCase):
    """直接造 session（不 join、不連線）：切句／播放／打斷都是純邏輯。"""

    def make(self, **overrides) -> DiscordVoiceSession:
        settings = dict(enabled=True, channel_id="vc", min_rms=320,
                        silence_s=0.5, min_utterance_s=0.3, max_utterance_s=3.0,
                        barge_in=True)
        settings.update(overrides)
        config = VoiceConfig(**settings)
        session = DiscordVoiceSession(config, None, log=self.logs.append)
        session.allowed_users = {OWNER_ID}
        session.voice_client = FakeVoiceClient()
        session._wav_to_pcm = staticmethod(lambda wav: b"\x00\x00" * 960)
        return session

    def setUp(self):
        self.logs: list[str] = []


class TestSpeechStartEdge(SessionFixture):
    def test_utterance_buffer_fires_on_rising_edge_only(self):
        fired: list[str] = []
        config = VoiceConfig(enabled=True, min_rms=320, silence_s=0.5)
        buffer = UtteranceBuffer(config, lambda _u: None, on_speech_start=fired.append)
        for _ in range(10):
            buffer.feed(OWNER_ID, loud_block())
        self.assertEqual(fired, [OWNER_ID], "一句話只叫一次（上升沿）")
        for _ in range(30):                                   # 0.6 s 靜音 ⇒ 收一句
            buffer.feed(OWNER_ID, bytes(len(loud_block())))
        for _ in range(5):
            buffer.feed(OWNER_ID, loud_block())
        self.assertEqual(len(fired), 2, "下一句再叫一次")

    def test_silero_edge_detector_fires_on_transition(self):
        try:
            from agent.adapter.silero_cut import SileroCutter
        except ImportError as exc:       # silero_cut 需要 numpy（image 裡有）；開發機沒裝就跳過
            self.skipTest(f"silero_cut 匯入失敗：{exc}")

        class FakeVad:
            def __init__(self):
                self.state = False

            def is_speech_detected(self):
                return self.state

        fired: list[int] = []
        cutter = SileroCutter.__new__(SileroCutter)      # 不需要模型，只測上升沿
        cutter.on_speech_start = lambda: fired.append(1)
        cutter._in_speech = False
        cutter.speech_starts = 0
        cutter.vad = FakeVad()
        cutter._note_edge()
        self.assertEqual(fired, [])
        cutter.vad.state = True
        cutter._note_edge()
        cutter._note_edge()
        self.assertEqual(fired, [1], "持續有語音不會重複叫")
        cutter.vad.state = False
        cutter._note_edge()
        cutter.vad.state = True
        cutter._note_edge()
        self.assertEqual(fired, [1, 1])
        self.assertEqual(cutter.speech_starts, 2)


class TestBargeIn(SessionFixture):
    def test_owner_speaking_stops_her_voice(self):
        session = self.make()
        session.playing = True
        seen: list[tuple] = []
        session.on_interrupt = lambda user, epoch: seen.append((user, epoch))
        self.assertTrue(session.note_speech_start(OWNER_ID))
        self.assertEqual(session.voice_client.stops, 1, "要叫 stop 停播")
        self.assertFalse(session.playing)
        self.assertTrue(session.tts_cancelled)
        self.assertEqual(session.interruptions, 1)
        self.assertEqual(seen, [(OWNER_ID, 1)])
        self.assertTrue(any("打斷" in line for line in self.logs), self.logs)

    def test_quiet_session_is_not_an_interruption(self):
        session = self.make()
        session.playing = False
        self.assertFalse(session.note_speech_start(OWNER_ID))
        self.assertEqual(session.voice_client.stops, 0)
        self.assertEqual(session.interruptions, 0)

    def test_stranger_cannot_interrupt(self):
        session = self.make()
        session.playing = True
        self.assertFalse(session.note_speech_start(OTHER_ID))
        self.assertEqual(session.voice_client.stops, 0, "白名單外的人不該能讓她閉嘴")
        self.assertTrue(session.playing)

    def test_switch_off_keeps_old_behaviour(self):
        session = self.make(barge_in=False)
        session.playing = True
        self.assertFalse(session.note_speech_start(OWNER_ID))
        self.assertEqual(session.voice_client.stops, 0)
        self.assertTrue(session.playing)

    def test_interrupt_hook_failure_still_stops_playback(self):
        session = self.make()
        session.playing = True

        def boom(_user, _epoch):
            raise RuntimeError("hook 壞了")

        session.on_interrupt = boom
        self.assertTrue(session.note_speech_start(OWNER_ID))
        self.assertEqual(session.voice_client.stops, 1)
        self.assertFalse(session.playing)


class TestStaleAudioIsDropped(SessionFixture):
    def setUp(self):
        super().setUp()
        try:
            import discord  # noqa: F401
        except Exception as exc:                          # noqa: BLE001
            self.skipTest(f"沒有 discord.py：{exc}")

    def test_audio_from_interrupted_turn_is_not_played(self):
        session = self.make()
        session._epoch = 3                       # 已經被打斷過 3 次
        session._publish({"audio": tone_wav()}, epoch=1)
        self.assertEqual(session.voice_client.plays, [], "舊世代的聲音不該播")
        session._publish({"audio": tone_wav()}, epoch=3)
        self.assertEqual(len(session.voice_client.plays), 1)
        self.assertTrue(session.playing)
        session.voice_client.after(None)         # 播完 ⇒ 狀態回乾淨
        self.assertFalse(session.playing)


class FakeClip:
    """`TtsPort` 的 clip 形狀（只要 `payload()`）。"""

    def __init__(self, text: str, seconds: float = 0.4) -> None:
        self.text = text
        self.seconds = seconds

    def payload(self) -> dict:
        return {"audio": "", "seconds": self.seconds, "text": self.text, "mock": False}


class FakeTts:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def speak(self, text: str, voice: str = "") -> list[FakeClip]:
        self.calls.append(text)
        return [FakeClip(text)]


class TestPlaybackQueue(SessionFixture):
    """兩段聲音（短回覆 ＋ 後續完整答案）不能互相頂掉。

    2026-10-22 實測：短回覆還在播時，後續答案直接 `play()` 會被 discord.py 拒
    （`Already playing audio`）⇒ 聽起來就是「有一段話沒被讀出來」。
    """

    def setUp(self):
        super().setUp()
        try:
            import discord  # noqa: F401
        except Exception as exc:                          # noqa: BLE001
            self.skipTest(f"沒有 discord.py：{exc}")

    def test_second_clip_waits_and_plays_after_first(self):
        session = self.make()
        session._publish({"audio": tone_wav()}, epoch=0)
        self.assertEqual(len(session.voice_client.plays), 1)
        session._publish({"audio": tone_wav()}, epoch=0)
        self.assertEqual(len(session.voice_client.plays), 1, "第一段還在播 ⇒ 第二段排隊，不是覆蓋")
        session.voice_client.after(None)                 # 第一段播完
        self.assertEqual(len(session.voice_client.plays), 2, "接著播第二段")
        session.voice_client.after(None)
        self.assertFalse(session.playing)

    def test_interrupt_drops_the_queue(self):
        session = self.make()
        session.allowed_users = {OWNER_ID}
        session._publish({"audio": tone_wav()}, epoch=0)
        session._publish({"audio": tone_wav()}, epoch=0)
        self.assertTrue(session.note_speech_start(OWNER_ID))
        self.assertEqual(len(session._pcm_queue), 0, "打斷＝她現在閉嘴，排隊的也丢掉")
        self.assertFalse(session.playing)


class TestSpeakerCancel(unittest.TestCase):
    def make_voice(self) -> VoiceService:
        return VoiceService(build_entities(), tts=FakeTts())

    def test_cancel_stops_further_sentences_and_releases_floor(self):
        voice = self.make_voice()
        speaker = voice.begin_stream("mira", thread_id="th_9", force=True,
                                      target="discord:vc-lobby")
        frames = speaker.speak("第一句")
        self.assertEqual(frames[0]["kind"], "speech")
        self.assertEqual(voice.floor.holder("mira"), "th_9")
        speaker.cancel()
        self.assertEqual(speaker.speak("第二句"), [], "取消後不該再合成")
        self.assertIsNone(voice.floor.holder("mira"), "取消要放掉發言權")
        self.assertTrue(speaker.cancelled)
        self.assertEqual(voice.tts.calls, ["第一句"], "取消後不該再叫 TTS")

    def test_interrupt_invalidates_existing_speaker(self):
        voice = self.make_voice()
        speaker = voice.begin_stream("mira", thread_id="th_9", force=True,
                                      target="discord:vc-lobby")
        self.assertEqual(speaker.speak("第一句")[0]["kind"], "speech")
        self.assertEqual(voice.floor.holder("mira"), "th_9")
        record = voice.interrupt("mira", principal="owner")
        self.assertEqual(record["epoch"], 1)
        self.assertEqual(record["floor_holder"], "th_9", "要回報原本是誰拿著發言權")
        self.assertIsNone(voice.floor.holder("mira"))
        self.assertEqual(speaker.speak("被打斷之後"), [])
        self.assertEqual(voice.tts.calls, ["第一句"], "被打斷之後不再合成")
        # 新的一輪照常能說話（打斷不是永久啞掉）
        fresh = voice.begin_stream("mira", thread_id="th_10", force=True,
                                   target="discord:vc-lobby")
        self.assertEqual(fresh.speak("新的回答")[0]["kind"], "speech")

    def test_other_entity_is_untouched(self):
        voice = self.make_voice()
        other = voice.begin_stream("aster", thread_id="th_r", force=True,
                                   target="discord:vc-lobby")
        other.speak("先說一句")
        voice.interrupt("mira")
        self.assertEqual(other.speak("我還在說")[0]["kind"], "speech",
                         "打斷一個角色不影響另一個（一實體一張嘴）")


class TestArbiterReleaseEntity(unittest.TestCase):
    def test_release_returns_previous_holder(self):
        floor = FloorArbiter()
        floor.acquire("mira", "th_1", priority=5)
        self.assertEqual(floor.release_entity("mira"), "th_1")
        self.assertIsNone(floor.holder("mira"))
        self.assertEqual(floor.release_entity("mira"), "", "本來就沒有 ⇒ 空字串")


if __name__ == "__main__":
    unittest.main(verbosity=2)
