"""音訊：48k 立體聲 PCM ↔ 16k WAV、能量（RMS）、按靜音把一段段話切出來（Utterance）。"""

from __future__ import annotations

import audioop
import io
import time
import wave
from dataclasses import dataclass, field

from agent.adapter.discord_voice.config import STT_RATE, VC_CHANNELS, VC_RATE, VoiceConfig


def pcm48_stereo_to_wav16(pcm: bytes) -> bytes:
    """48k 立體 → 16k 單聲道 wav（whisper 只吃這種最省事）。"""
    mono = audioop.tomono(pcm, 2, 0.5, 0.5)
    converted, _ = audioop.ratecv(mono, 2, 1, VC_RATE, STT_RATE, None)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(STT_RATE)
        target.writeframes(converted)
    return buffer.getvalue()


def _pcm16_to_wav(pcm: bytes, rate: int = STT_RATE, channels: int = 1) -> bytes:
    """16 k mono s16le → wav（STT 吃 wav 最省事）。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setnchannels(channels)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(pcm)
    return buffer.getvalue()


def rms_of(pcm: bytes) -> int:
    return audioop.rms(pcm, 2) if pcm else 0


@dataclass
class Utterance:
    """一句講完的話：誰說的、多久、音檔。"""

    user_id: str
    wav: bytes
    seconds: float
    at: float = field(default_factory=time.time)


class UtteranceBuffer:
    """把一串音訊塊切成「一句一句」。純邏輯，可離線測（不碰 discord）。"""

    def __init__(self, config: VoiceConfig, on_utterance, on_speech_start=None) -> None:
        self.config = config
        self.on_utterance = on_utterance
        #: 開口那一刻（能量法的上升沿；silero 那條路用 cutter 自己的回呼）。
        self.on_speech_start = on_speech_start
        self._pcm: dict[str, bytearray] = {}
        self._silence: dict[str, float] = {}
        self._talking: dict[str, bool] = {}
        #: 只算「有聲音」的時間（不算句尾靜音）——短雜訊的判定要看這個，不是總長。
        self._speech: dict[str, float] = {}

    def feed(self, user_id: str, pcm: bytes, *, seconds: float = 0.02) -> bool:
        """餵一塊音訊；回「有沒有送出一句」。"""
        user_id = str(user_id)
        level = rms_of(pcm)
        buffer = self._pcm.setdefault(user_id, bytearray())
        if level >= self.config.min_rms:
            if not self._talking.get(user_id) and self.on_speech_start is not None:
                try:
                    self.on_speech_start(user_id)
                except Exception:                      # noqa: BLE001
                    pass
            buffer.extend(pcm)
            self._talking[user_id] = True
            self._silence[user_id] = 0.0
            self._speech[user_id] = self._speech.get(user_id, 0.0) + seconds
        else:
            if not self._talking.get(user_id):
                return False
            # 靜音也要收進去（句尾自然衰減留著，轉寫更準）
            buffer.extend(pcm)
            self._silence[user_id] = self._silence.get(user_id, 0.0) + seconds
        length_s = len(buffer) / (VC_RATE * 2 * VC_CHANNELS)
        ends = (self._silence.get(user_id, 0.0) >= self.config.silence_s
                or length_s >= self.config.max_utterance_s)
        if not ends:
            return False
        clip = bytes(buffer)
        speech_s = self._speech.get(user_id, 0.0)
        buffer.clear()
        self._talking[user_id] = False
        self._silence[user_id] = 0.0
        self._speech[user_id] = 0.0
        # 短雜訊：看「真的有聲音」的長度，不看含句尾靜音的總長
        if speech_s < self.config.min_utterance_s:
            return False
        self.on_utterance(Utterance(user_id=user_id, wav=pcm48_stereo_to_wav16(clip),
                                    seconds=round(length_s, 2)))
        return True
