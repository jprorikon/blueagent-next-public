"""語音動作的狀態對象：一次通話 / 一段語音輸出。

語音不是 UI 的朗讀開關，而是她可以自己發起的動作：

    voice.call(end="discord")   → 一次 VoiceSession（粘性：通話期間回覆都走這裡）
    voice.speak(target=...)     → 對當前回覆做一次渲染
    voice.hangup()              → 解綁

`VoiceSession` 是這個狀態機的唯一真相；`VoiceService` 管生命週期與 idempotency。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum


class VoiceState(str, Enum):
    DIALING = "dialing"
    CONNECTED = "connected"
    ENDED = "ended"
    FAILED = "failed"


@dataclass
class VoiceSession:
    id: str
    entity: str
    end: str                 # discord / phone / ...
    target: str              # 具體 source/端（例如 discord:vc-lobby）
    state: VoiceState = VoiceState.DIALING
    reason: str = ""
    principal: str = ""
    thread_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: 端末最後一次「還在線」的時間（App 通話的心跳／每一句話都會刷新）
    last_seen: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)

    @classmethod
    def create(cls, entity: str, end: str, target: str, *, reason: str = "",
               principal: str = "", thread_id: str = "") -> "VoiceSession":
        now = time.time()
        return cls(id=f"call_{secrets.token_hex(6)}", entity=str(entity), end=str(end),
                   target=str(target), state=VoiceState.CONNECTED, reason=str(reason),
                   principal=str(principal), thread_id=str(thread_id),
                   created_at=now, updated_at=now, last_seen=now)

    @property
    def active(self) -> bool:
        return self.state in {VoiceState.DIALING, VoiceState.CONNECTED}

    def duration_s(self, now: float | None = None) -> float:
        at = float(now if now is not None else time.time())
        return max(0.0, at - self.created_at)

    def touch(self, now: float | None = None) -> None:
        self.last_seen = float(now if now is not None else time.time())

    def idle_s(self, now: float | None = None) -> float:
        at = float(now if now is not None else time.time())
        return max(0.0, at - self.last_seen)

    def end_call(self) -> None:
        self.state = VoiceState.ENDED
        self.updated_at = time.time()

    def to_dict(self) -> dict:
        now = time.time()
        return {
            "id": self.id, "entity": self.entity, "end": self.end,
            "target": self.target, "state": self.state.value,
            "reason": self.reason, "principal": self.principal,
            "thread_id": self.thread_id,
            "created_at": round(self.created_at, 3),
            "duration_s": round(self.duration_s(now), 1),
        }
