"""Discord 語音頻道：進頻道聽、斷句、STT、回話（聲音也回進頻道）。

與文字源的關係：**不另開一條管道**。語音進來一樣走 `DiscordSource.handle()`（因此一樣經過
注意力層、記憶 ACL、委任權限），差別只在兩端：

  · 進：PCM 48k 立體 → 16k 單聲道 → faster-whisper → 文字
  · 出：她的 wav → ffmpeg → 播進語音頻道（同時把文字貼在頻道的文字聊天裡）

斷句用「能量 + 靜音長度」而不用模型：VC 裡一句話中間的停頓很短（~200ms），
句尾通常 > 600ms。參數都可調，寧可多切一刀（多一次轉寫）也不要黏成一長句。

⚠️ 進頻道要 `discord-ext-voice-recv`；沒有就整個跳過（文字源不受影響）。

按職責拆開（2026-10-25 review P3）：設定（config）、音訊轉換與斷句（audio）、在場與進出頻道（presence）、
接收與打斷（receive）、播放與發佈（playback）、工作階段本體（session）。
`from adapter.discord_voice import X` 照用。
"""

from __future__ import annotations

from agent.adapter.discord_voice.config import VC_RATE, VC_CHANNELS, STT_RATE, VoiceConfig  # noqa: F401
from agent.adapter.discord_voice.audio import pcm48_stereo_to_wav16, _pcm16_to_wav, rms_of, Utterance, UtteranceBuffer  # noqa: F401
from agent.adapter.discord_voice.session import DiscordVoiceSession, build_voice  # noqa: F401

__all__ = ["DiscordVoiceSession", "STT_RATE", "Utterance", "UtteranceBuffer", "VC_CHANNELS", "VC_RATE", "VoiceConfig", "build_voice", "pcm48_stereo_to_wav16", "rms_of"]
