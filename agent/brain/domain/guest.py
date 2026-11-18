"""從屬客戶端（Guest）的加入申請與關係聲明。

關鍵語義：**Guest 只能「聲明」自己想和 master 是什麼關係，不能自己定案。**
最終關係由 owner 在審核時決定；申請裡同時保留 declared（原話）與 final（拍板）。
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum

from agent.brain.domain.ids import norm

RELATION_KINDS = ("family", "friend", "colleague", "classmate", "partner",
                 "acquaintance", "stranger", "other")


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class RelationDeclaration:
    kind: str = "other"
    label: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        kind = norm(self.kind, fallback="other")
        object.__setattr__(self, "kind", kind if kind in RELATION_KINDS else "other")
        object.__setattr__(self, "label", str(self.label or "").strip()[:40])
        object.__setattr__(self, "note", str(self.note or "").strip()[:300])

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "note": self.note}


@dataclass(frozen=True, slots=True)
class GuestRelationProposal:
    """新 guest 對某個已有 guest 的關係聲明（最終仍由主人拍板）。"""

    other_discord_id: str
    other_principal: str = ""
    kind: str = "acquaintance"
    label: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        kind = norm(self.kind, fallback="acquaintance")
        object.__setattr__(self, "kind", kind if kind in RELATION_KINDS else "other")
        object.__setattr__(self, "other_discord_id", str(self.other_discord_id or "").strip())
        object.__setattr__(self, "other_principal", norm(self.other_principal, fallback=""))
        object.__setattr__(self, "label", str(self.label or "").strip()[:40])
        object.__setattr__(self, "note", str(self.note or "").strip()[:300])

    def to_dict(self) -> dict:
        return {"other_discord_id": self.other_discord_id,
                "other_principal": self.other_principal, "kind": self.kind,
                "label": self.label, "note": self.note}

    @classmethod
    def from_dict(cls, raw: dict) -> "GuestRelationProposal":
        return cls(other_discord_id=str(raw.get("other_discord_id") or ""),
                   other_principal=str(raw.get("other_principal") or ""),
                   kind=str(raw.get("kind") or "acquaintance"),
                   label=str(raw.get("label") or ""),
                   note=str(raw.get("note") or ""))


def hash_guest_token(token: str) -> str:
    """與帳號層（`runtime/identity/accounts.hash_token`）同一個算法：sha256 hex。"""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


@dataclass
class GuestRequest:
    id: str
    invite_code: str
    discord_id: str
    display_name: str
    declared: RelationDeclaration
    status: RequestStatus = RequestStatus.PENDING
    request_secret: str = field(default_factory=lambda: f"req_{secrets.token_urlsafe(18)}")
    created_at: float = field(default_factory=time.time)
    decided_at: float = 0.0
    decided_by: str = ""
    final: RelationDeclaration | None = None
    trust: float = 0.5
    capabilities: tuple[str, ...] = ("chat",)
    principal_id: str = ""
    #: 明文 token：**只在「批准了、guest 還沒來拿」那段時間**有值（`/api/guest/status` 拿一次就清掉）。
    #: 長期只留 `token_sha256`（2026-10-31：以前明文一直躺在 guests.json，檔案被看到＝拿到所有 guest 的 token）。
    token: str = ""
    token_sha256: str = ""
    #: 對已存在 guest 的關係聲明／主人拍板（兩段都保留）
    declared_guests: tuple[GuestRelationProposal, ...] = ()
    final_guests: tuple[GuestRelationProposal, ...] = ()

    def set_token(self, plain: str) -> None:
        """換 token：雜湊永久留著，明文先留著等 guest 來拿（`deliver_token` 之後清掉）。"""
        self.token = str(plain or "")
        self.token_sha256 = hash_guest_token(self.token) if self.token else ""

    def remember_hash(self, plain: str) -> None:
        """只記雜湊（明文已經在別處交出去了，例如帳號層簽發時回給主人那一次）。"""
        self.token = ""
        self.token_sha256 = hash_guest_token(plain) if plain else ""

    def clear_token(self) -> None:
        self.token = ""
        self.token_sha256 = ""

    @classmethod
    def create(cls, invite_code: str, discord_id: str, display_name: str,
               declared: RelationDeclaration, now: float | None = None,
               guest_relations=()) -> "GuestRequest":
        at = float(now if now is not None else time.time())
        return cls(id=f"guestreq_{secrets.token_hex(5)}", invite_code=str(invite_code),
                   discord_id=str(discord_id).strip(), display_name=str(display_name).strip(),
                   declared=declared, created_at=at,
                   declared_guests=tuple(guest_relations))

    def to_dict(self) -> dict:
        return {"id": self.id, "invite_code": self.invite_code,
                "discord_id": self.discord_id, "display_name": self.display_name,
                "declared": self.declared.to_dict(), "status": self.status.value,
                "request_secret": self.request_secret,
                "created_at": round(self.created_at, 3),
                "decided_at": round(self.decided_at, 3),
                "decided_by": self.decided_by,
                "final": self.final.to_dict() if self.final else None,
                "trust": self.trust, "capabilities": list(self.capabilities),
                "principal_id": self.principal_id, "token": self.token,
                "token_sha256": self.token_sha256,
                "declared_guests": [item.to_dict() for item in self.declared_guests],
                "final_guests": [item.to_dict() for item in self.final_guests]}

    @classmethod
    def from_dict(cls, raw: dict) -> "GuestRequest":
        final = raw.get("final")
        return cls(
            id=str(raw.get("id") or f"guestreq_{secrets.token_hex(5)}"),
            invite_code=str(raw.get("invite_code") or ""),
            discord_id=str(raw.get("discord_id") or ""),
            display_name=str(raw.get("display_name") or ""),
            declared=RelationDeclaration(**(raw.get("declared") or {})),
            status=RequestStatus(str(raw.get("status") or "pending")),
            request_secret=str(raw.get("request_secret") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            decided_at=float(raw.get("decided_at") or 0.0),
            decided_by=str(raw.get("decided_by") or ""),
            final=RelationDeclaration(**final) if isinstance(final, dict) else None,
            trust=float(raw.get("trust") or 0.5),
            capabilities=tuple(str(cap) for cap in (raw.get("capabilities") or ("chat",))),
            principal_id=str(raw.get("principal_id") or ""),
            # 舊檔只有明文：載入時換成雜湊、明文丟掉（那些 guest 早就拿到 token 了）
            token=str(raw.get("token") or "") if raw.get("token_sha256") else "",
            token_sha256=str(raw.get("token_sha256") or "")
            or (hash_guest_token(raw.get("token")) if raw.get("token") else ""),
            declared_guests=tuple(GuestRelationProposal.from_dict(item)
                                  for item in (raw.get("declared_guests") or ())),
            final_guests=tuple(GuestRelationProposal.from_dict(item)
                               for item in (raw.get("final_guests") or ())),
        )
