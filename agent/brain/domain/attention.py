"""群聊注意力：焦點租約、四級動作、評分參數。

這一層只描述「要不要接、用什麼顆粒度接」，不執行；執行在 `runtime/attention.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.brain.domain.decision import Engagement


@dataclass
class FocusLease:
    """Agent 回過 A 之後，A 的追問在 ttl/輪數內不再需要 @。"""

    entity: str
    thread_id: str
    principal: str
    created_at: float
    expires_at: float
    turns_left: int
    group: str = ""

    def active(self, now: float) -> bool:
        return now < self.expires_at and self.turns_left > 0

    def to_dict(self) -> dict:
        return {"entity": self.entity, "thread": self.thread_id,
                "principal": self.principal, "group": self.group,
                "created_at": round(self.created_at, 3),
                "expires_at": round(self.expires_at, 3),
                "turns_left": self.turns_left}

    @classmethod
    def from_dict(cls, raw: dict) -> "FocusLease":
        return cls(entity=str(raw.get("entity") or ""),
                   thread_id=str(raw.get("thread") or raw.get("thread_id") or ""),
                   principal=str(raw.get("principal") or ""),
                   group=str(raw.get("group") or ""),
                   created_at=float(raw.get("created_at") or 0.0),
                   expires_at=float(raw.get("expires_at") or 0.0),
                   turns_left=int(raw.get("turns_left") or 0))


@dataclass(frozen=True)
class AttentionConfig:
    lease_ttl_s: float = 90.0
    lease_turns: int = 3
    yield_ms: int = 8000
    voice_yield_ms: int = 1200
    reply_threshold: float = 0.62
    yield_threshold: float = 0.45
    react_threshold: float = 0.30
    weight_target: float = 0.35
    weight_value: float = 0.30
    weight_role: float = 0.20
    weight_intrusion: float = 0.25
    #: 影子模式：照樣計算/記錄，但不改變實際動作（用來校準權重）。
    shadow: bool = False


@dataclass(frozen=True)
class AttentionDecision:
    action: Engagement
    score: float
    reason: str
    lease_id: str = ""
    yield_ms: int = 0


@dataclass
class AttentionFeatures:
    target: float = 0.0
    value: float = 0.0
    role: float = 0.0
    intrusion: float = 0.0
    memo: bool = False

    def score(self, config: AttentionConfig) -> float:
        return (config.weight_target * self.target
                + config.weight_value * self.value
                + config.weight_role * self.role
                - config.weight_intrusion * self.intrusion)
