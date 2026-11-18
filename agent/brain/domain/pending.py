"""YIELD 待辦：她決定「先禮讓一下」時，把要做的事寫成可恢復的記錄。"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field


@dataclass
class PendingIntervention:
    id: str
    key: str
    text: str
    entity: str
    principal: str
    group: bool
    mention: bool
    addressed_to: str
    participants: tuple[str, ...]
    due_at: float
    yield_ms: int
    created_at: float = field(default_factory=time.time)
    #: 原回合的路由（REVIEW 2026-11-08）：重放要回到**同一條線、同一個來源**。
    #: 以前不存 ⇒ 重放一律按預設路由（群聊＝`group:discord-lobby`／`discord:<principal>`）。
    source_override: str = ""
    scope_override: str = ""
    scene_override: str = ""

    @classmethod
    def create(cls, *, key: str, text: str, entity: str, principal: str,
               group: bool, mention: bool, addressed_to: str,
               participants, yield_ms: int, source_override: str = "",
               scope_override: str = "", scene_override: str = "") -> "PendingIntervention":
        now = time.time()
        return cls(id=f"pend_{secrets.token_hex(5)}", key=str(key), text=str(text),
                   entity=str(entity), principal=str(principal), group=bool(group),
                   mention=bool(mention), addressed_to=str(addressed_to or ""),
                   participants=tuple(participants or ()), due_at=now + max(0.05, yield_ms / 1000),
                   yield_ms=int(yield_ms), created_at=now,
                   source_override=str(source_override or ""),
                   scope_override=str(scope_override or ""),
                   scene_override=str(scene_override or ""))

    def to_dict(self) -> dict:
        return {"id": self.id, "key": self.key, "text": self.text,
                "entity": self.entity, "principal": self.principal,
                "group": self.group, "mention": self.mention,
                "addressed_to": self.addressed_to,
                "participants": list(self.participants),
                "due_at": round(self.due_at, 3), "yield_ms": self.yield_ms,
                "created_at": round(self.created_at, 3),
                "source_override": self.source_override,
                "scope_override": self.scope_override,
                "scene_override": self.scene_override}

    @classmethod
    def from_dict(cls, raw: dict) -> "PendingIntervention":
        return cls(id=str(raw.get("id") or f"pend_{secrets.token_hex(5)}"),
                   key=str(raw.get("key") or ""), text=str(raw.get("text") or ""),
                   entity=str(raw.get("entity") or "mira"),
                   principal=str(raw.get("principal") or ""),
                   group=bool(raw.get("group")), mention=bool(raw.get("mention")),
                   addressed_to=str(raw.get("addressed_to") or ""),
                   participants=tuple(str(who) for who in (raw.get("participants") or ())),
                   due_at=float(raw.get("due_at") or 0.0),
                   yield_ms=int(raw.get("yield_ms") or 0),
                   created_at=float(raw.get("created_at") or 0.0),
                   source_override=str(raw.get("source_override") or ""),
                   scope_override=str(raw.get("scope_override") or ""),
                   scene_override=str(raw.get("scene_override") or ""))
