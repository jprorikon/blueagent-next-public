"""語音頻道的設定（`BLUEAGENT_DISCORD_VC*`）與取樣率常數。"""

from __future__ import annotations

from dataclasses import dataclass, field


#: VC 進來的音訊規格（Discord 固定）。
VC_RATE = 48000


VC_CHANNELS = 2


#: 送進 whisper 的規格。
STT_RATE = 16000


@dataclass
class VoiceConfig:
    enabled: bool = False
    channel_id: str = ""
    #: 判定「有聲音」的 RMS 門檻（0–32767）。太低會被鍵盤聲觸發，太高聽不到小聲說話。
    min_rms: int = 320
    #: 句尾靜音多久算一句講完（秒）。
    silence_s: float = 0.7
    #: 一句最短長度（秒）：短於這個當雜訊丟掉。
    min_utterance_s: float = 0.35
    #: 一句最長（秒）：超過就強制切（避免一個人自顧自講三分鐘）。
    max_utterance_s: float = 15.0
    #: 回話要不要播進頻道（關掉＝只貼文字）。
    speak_in_voice: bool = True
    #: silero 判「這是語音」的機率門檻（不是能量門檻）。
    vad_threshold: float = 0.5
    #: 人一開口就打斷她（barge-in）。關掉＝她的聲音會跟主人重疊（舊行為）。
    barge_in: bool = True

    @classmethod
    def from_env(cls, values) -> "VoiceConfig":
        def flag(name: str, default: str = "0") -> bool:
            return str(values.get(name) or default).strip().lower() in {"1", "on", "true", "yes"}

        return cls(
            enabled=flag("BLUEAGENT_DISCORD_VC"),
            channel_id=str(values.get("VOICE_CHANNEL_ID") or "").strip(),
            min_rms=int(values.get("BLUEAGENT_VC_MIN_RMS") or 320),
            silence_s=float(values.get("BLUEAGENT_VC_SILENCE_S") or 0.7),
            min_utterance_s=float(values.get("BLUEAGENT_VC_MIN_S") or 0.35),
            max_utterance_s=float(values.get("BLUEAGENT_VC_MAX_S") or 15.0),
            speak_in_voice=flag("BLUEAGENT_VC_SPEAK", "1"),
            vad_threshold=float(values.get("BLUEAGENT_VAD_THRESHOLD") or 0.5),
            barge_in=flag("BLUEAGENT_VC_BARGE_IN", "1"),
        )
