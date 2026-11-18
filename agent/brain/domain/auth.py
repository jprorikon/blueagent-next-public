"""认证后的调用者：**principal 是谁、以什么角色在说话**。

`principal` 回答「是谁」，`role` 回答「能做什么」。两者不能从同一个自报字段来：
principal 由 token／Discord user id／设备配对解析，role 由签发 token 的那一方决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from agent.brain.domain.ids import norm


class Role(str, Enum):
    OWNER = "owner"
    GUEST = "guest"
    #: 服務帳號（某個源的某一顆 bot，owner 2026-11-16）：不是人，只能替綁定好的外部身分發回合
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class AuthContext:
    principal: str
    role: Role = Role.GUEST
    capabilities: frozenset[str] = frozenset({"chat"})
    #: token 的稳定标识（不是 token 本身）；用于审计与撤销。
    token_id: str = ""
    #: 仅在进程内转发给同一调用者的 App Server；永不序列化、审计或写日志。
    credential: str = field(default="", repr=False, compare=False)
    #: 这个帐号的哪一台端末（`primary`／`dev_…`；多设备配对，2026-11-16）
    device: str = ""
    #: 服务帐号绑定的源与实体（`discord`／`aster`）；人类帐号为空
    source: str = ""
    entity: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "principal", norm(self.principal, fallback="guest"))
        object.__setattr__(self, "role", Role(self.role))
        object.__setattr__(self, "capabilities",
                           frozenset(str(cap) for cap in self.capabilities if str(cap)))

    @property
    def is_owner(self) -> bool:
        return self.role is Role.OWNER

    def can(self, capability: str) -> bool:
        return self.is_owner or str(capability) in self.capabilities

    def to_dict(self) -> dict:
        """给客户端看的身份摘要；**永远不包含 token**。"""
        return {"principal": self.principal, "role": self.role.value,
                "capabilities": sorted(self.capabilities),
                "token_id": self.token_id, "device": self.device}


__all__ = ["AuthContext", "Role"]
