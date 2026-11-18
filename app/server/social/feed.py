"""公共区通用摘要、稳定游标与分页；不读取 Agent 活动或作品。"""

from __future__ import annotations

from app.server.social.models import Post


def excerpt(text: str, limit: int = 120) -> tuple[str, bool]:
    body = str(text or "").strip()
    return (body[:limit], len(body) > limit)


def encode_cursor(post: Post) -> str:
    return f"{float(post.at):.3f}|{post.id}"


def decode_cursor(value: str) -> tuple[float, str] | None:
    try:
        at, post_id = str(value or "").rsplit("|", 1)
        return float(at), str(post_id)
    except (TypeError, ValueError):
        return None


def page(posts: list[Post], *, before: str = "", limit: int = 20,
         since: float = 0.0, authors: set[str] | None = None) -> dict:
    """稳定倒序分页：同秒时以 id 决胜，三页合并不重不漏。"""
    limit = max(1, min(60, int(limit or 20)))
    cursor = decode_cursor(before) if before else None
    if before and cursor is None:
        raise ValueError("bad_cursor")
    rows = [post for post in posts if not post.parent_id and not post.deleted_at
            and post.visibility == "public" and post.at >= float(since or 0.0)
            and (not authors or post.author_id in authors)]
    if cursor is not None:
        at, post_id = cursor
        rows = [post for post in rows if (post.at, post.id) < (at, post_id)]
    rows.sort(key=lambda post: (post.at, post.id), reverse=True)
    selected = rows[:limit]
    return {"posts": selected,
            "next_cursor": encode_cursor(selected[-1]) if len(rows) > limit and selected else ""}


__all__ = ["decode_cursor", "encode_cursor", "excerpt", "page"]
