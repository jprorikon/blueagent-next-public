"""Agent 侧平台线协议；只含远程调用所需类型，不导入 App Server 实现。"""

from __future__ import annotations

from dataclasses import dataclass


class BoardError(Exception):
    """把 App Server 的业务错误安全映射到旧 HTTP／stdio 入口。"""

    def __init__(self, status: int, code: str, **extra) -> None:
        super().__init__(code)
        self.status = int(status)
        self.code = str(code)
        self.extra = dict(extra)

    def payload(self) -> dict:
        return {"error": self.code, **self.extra}


@dataclass(frozen=True, slots=True)
class Actor:
    id: str
    kind: str
    display: str
    visible_entities: frozenset[str] | None = None

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner"


__all__ = ["Actor", "BoardError"]
