"""平台公共区模型：统一表示手动投稿、回复与外部内容的只读投影。"""

from __future__ import annotations

from dataclasses import dataclass, field


class BoardError(Exception):
    """可安全映射到 HTTP／MCP 的公共区业务错误。"""

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
    kind: str                       # entity | owner | guest
    display: str
    #: None＝全部公开角色；客人绑定时只允许这一组角色的投影，人的公开帖不受影响。
    visible_entities: frozenset[str] | None = None

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner"


@dataclass(slots=True)
class Post:
    id: str
    at: float
    author_kind: str                 # entity | owner | guest
    author_id: str
    display: str
    body: str
    origin: str = "manual"          # manual | projection
    kind: str = "post"              # post | reply | work | diary | mutter
    media: list[str] = field(default_factory=list)
    visibility: str = "public"
    parent_id: str = ""
    source_ref: str = ""
    deleted_at: float = 0.0
    deleted_by: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "Post":
        media = row.get("media") or []
        if isinstance(media, str):
            import json
            try:
                media = json.loads(media)
            except ValueError:
                media = []
        return cls(
            id=str(row.get("id") or ""), at=float(row.get("at") or 0.0),
            author_kind=str(row.get("author_kind") or ""),
            author_id=str(row.get("author_id") or ""),
            display=str(row.get("display") or row.get("author_id") or ""),
            body=str(row.get("body") or ""), origin=str(row.get("origin") or "manual"),
            kind=str(row.get("kind") or "post"),
            media=[str(item) for item in media if str(item)],
            visibility=str(row.get("visibility") or "public"),
            parent_id=str(row.get("parent_id") or ""),
            source_ref=str(row.get("source_ref") or ""),
            deleted_at=float(row.get("deleted_at") or 0.0),
            deleted_by=str(row.get("deleted_by") or ""),
        )

    def to_row(self) -> dict:
        return {
            "id": self.id, "at": round(float(self.at), 3),
            "author_kind": self.author_kind, "author_id": self.author_id,
            "display": self.display, "origin": self.origin,
            "source_ref": self.source_ref or None, "body": self.body,
            "media": list(self.media), "visibility": self.visibility,
            "kind": self.kind, "parent_id": self.parent_id or None,
            "deleted_at": self.deleted_at or None,
            "deleted_by": self.deleted_by or None,
        }


__all__ = ["Actor", "BoardError", "Post"]
