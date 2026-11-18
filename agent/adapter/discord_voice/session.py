"""語音工作階段本體：一個頻道一個 session（由在場、接收、播放三個 mixin 組成）＋ build_voice。"""

from __future__ import annotations

import os
import threading
from collections import deque
from agent.brain.runtime.voice.vc_presence import (ASK, JOIN, LEAVE, PresenceConfig,
                                                VCPresence)

from agent.adapter.discord_voice.audio import UtteranceBuffer
from agent.adapter.discord_voice.config import VoiceConfig
from agent.adapter.discord_voice.playback import VoicePlayback
from agent.adapter.discord_voice.presence import VoicePresence
from agent.adapter.discord_voice.receive import VoiceReceive


class DiscordVoiceSession(VoicePresence, VoiceReceive, VoicePlayback):
    """把 VC 的音訊接到 `DiscordSource`，並把她的聲音播回頻道。"""

    def __init__(self, config: VoiceConfig, source, *, log=None,
                 presence_config: PresenceConfig | None = None) -> None:
        self.config = config
        self.source = source
        self._log = log or (lambda _message: None)
        self.voice_client = None
        self.receiver = None
        #: 只收這些人的音（主人的 Discord id）；空＝不設限
        self.allowed_users = {str(item) for item in
                              (getattr(source, "config", None).owner_ids
                               if source is not None and getattr(source, "config", None)
                               else ())}
        # 切句：優先用 **silero-vad**（現役同一顆）；沒有模型/sherpa 才退回能量法
        self.buffer = None
        self.cutter = None
        try:
            from agent.adapter.silero_cut import SileroCutter
            self.cutter = SileroCutter(threshold=config.vad_threshold,
                                       on_speech_start=self._on_speech_edge)
        except Exception as exc:                      # noqa: BLE001
            self._log(f"vc: silero 不可用（{type(exc).__name__}: {exc}）⇒ 用能量切句")
            self.buffer = UtteranceBuffer(config, self.on_utterance,
                                          on_speech_start=self._on_speech_edge_user)
        self.last_error = ""
        # ── 碎片合併（owner 2026-10-22「感觉对话是混乱的」）────────────────────
        # 症狀：他把一句話講完，切句器卻切成「。」「しま。」「で。」「The.」好幾段，
        # 每段各起一輪 ⇒ 她連珠炮回問，對話看起來一片混亂。
        # 做法：同一人、間隔 < `merge_gap_s` 的碎片先**併成一句**再送進去；純雜訊丢掉。
        self.merge_gap_s = float(os.environ.get("BLUEAGENT_VC_MERGE_GAP_S") or 1.2)
        #: 最後一片判成「沒講完」⇒ 再多等這麼久（`adapters/end_judge.py`；0＝不多等）
        self.hold_extra_s = float(os.environ.get("BLUEAGENT_VC_HOLD_S") or 1.5)
        self._pending_incomplete: dict[str, bool] = {}
        self._pending: dict[str, list[str]] = {}
        self._pending_at: dict[str, float] = {}
        self._pending_lock = threading.RLock()
        self.merged = 0                    # 併掉幾次（診斷）
        self.noise_dropped = 0             # 丢掉幾片雜訊
        self.utterances = 0
        self.frames = 0            # 收到的音訊塊數（診斷用）
        self.packets = 0           # socket 層收到的 UDP 封包數（診斷用）
        self.joined = False
        self._active_speaker = ""   # silero 這條緩衝目前是誰在講
        # ── 打斷（barge-in）：她的聲音在播、主人一開口就停 ────────────────────
        #: 這一刻是不是在播她的聲音（`play()` 之後、`after` 之前）。
        self.playing = False
        #: 被打斷過的那一段聲音作廢（端末／呼叫端不要再播）。
        self.tts_cancelled = False
        self.interruptions = 0
        #: 打斷世代：`_publish` 送出前比對，舊世代的那段聲音直接丢。
        self._epoch = 0
        #: 待播的聲音（依序）：短回覆還在播時，後續完整答案要排隊，不能直接 `play()`。
        self._pcm_queue: deque[bytes] = deque()
        self._play_lock = threading.RLock()
        #: 打斷時的通知（`(user_id, epoch)`）：接線端拿去叫 runtime 收嘴＋記一筆。
        self.on_interrupt = None
        # ── 在場決策（owner 2026-10-22：什麼時候進去、什麼時候自己退）──────────
        owner_ids = {str(item) for item in
                     (getattr(getattr(source, "config", None), "owner_ids", ()) or ())}
        self.presence = VCPresence(
            presence_config or PresenceConfig(channel_id=config.channel_id),
            allowed=owner_ids)
        self._client = None
        self._guild_id = ""
        self._loop = None
        self._presence_stop = threading.Event()
        self._presence_thread: threading.Thread | None = None

































    def health(self) -> dict:
        return {"enabled": self.config.enabled, "joined": self.joined,
                "channel": self.config.channel_id, "utterances": self.utterances,
                "frames": self.frames,
                "playing": self.playing, "barge_in": self.config.barge_in,
                "interruptions": self.interruptions, "tts_cancelled": self.tts_cancelled,
                "presence": self.presence.snapshot(),
                "receiver": (self.receiver.health() if self.receiver is not None else None),
                "error": self.last_error}


def build_voice(config_env, source, *, log=None) -> DiscordVoiceSession | None:
    """有開 VC 且有頻道 id 才建 session（否則整個跳過，文字源不受影響）。"""
    config = VoiceConfig.from_env(config_env)
    if not config.enabled or not config.channel_id:
        return None
    return DiscordVoiceSession(config, source, log=log,
                               presence_config=PresenceConfig.from_env(config_env))
