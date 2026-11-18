"""公共区的客户端 HTTP 面：`/v1/feed`、`/v1/posts…`，与 MCP 的 feed/get/post/reply/like/delete 同一实现。

为什么有这一支（2026-11-08，PENDING B.App.8）：手机端以前读写公共区都经 Agent 的 `/api/feed`／`/api/post`
兼容入口——Agent 拿调用者的 token 再转调本服务的 MCP ⇒ Agent 挂了公共区就跟着挂，和 ADR
「App 自己持有数据」不一致。现在客户端直连这里，Agent 只剩 MCP 一条路。

  · 参数一律交给 `mcp.call()`：作者别名解析、幂等键、可见域、错误码都和 MCP 完全一样，不另写第二份。
  · 角色账号（entity）只读不写：她发帖／回帖／点赞的每日上限与计价在 Agent 的 MCP 闸那侧，
    HTTP 不给她一条绕过闸的路（同 `/v1/me/mood` 的 `mood_use_mcp`）。
  · `source_ref`／`occurred_at` 只给迁移与 Agent 发布用，客户端传了也不收。

路由：
    GET    /v1/feed?limit=&before=&since=&authors=a,b
    GET    /v1/posts/<id>
    POST   /v1/posts                 {text, media, client_mutation_id}
    POST   /v1/posts/<id>/replies    {text, media, client_mutation_id}
    POST   /v1/posts/<id>/like
    DELETE /v1/posts/<id>
回 `None` 表示「不是我的路由」，由呼叫端回 404。
"""

from __future__ import annotations

from app.server import mcp
from app.server.social import Actor, BoardError

POSTS = "/v1/posts"


def _split(path: str) -> tuple[str, str]:
    """`/v1/posts/<id>[/<action>]` → (id, action)；不是这个形状回 ("", "")。"""
    if not path.startswith(POSTS + "/"):
        return "", ""
    post_id, _, action = path[len(POSTS) + 1:].strip("/").partition("/")
    if not post_id or "/" in action:
        return "", ""
    return post_id, action


def _call(services, actor: Actor, name: str, args: dict) -> dict:
    return mcp.call(services.board, actor, name, args, accounts=services.accounts)


def _writer(actor: Actor) -> None:
    if actor.kind == "entity":
        raise BoardError(403, "board_use_mcp")


def _content(body: dict) -> dict:
    return {"text": body.get("text") if "text" in body else body.get("body"),
            "media": body.get("media") or body.get("media_ids") or [],
            "client_mutation_id": str(body.get("client_mutation_id") or "")}


def get(services, actor: Actor, path: str, query: dict) -> dict | None:
    def first(key: str, default: str = "") -> str:
        return str((query.get(key) or [default])[0] or default)

    if path == "/v1/feed":
        args = {"limit": int(first("limit", "20")), "before": first("before"),
                "since": float(first("since", "0"))}
        authors = [item.strip() for item in (first("authors") or first("author")).split(",")
                   if item.strip()]
        if authors:
            args["authors"] = authors
        return _call(services, actor, "feed", args)
    post_id, action = _split(path)
    if post_id and not action:
        return _call(services, actor, "get", {"id": post_id})
    return None


def post(services, actor: Actor, path: str, body: dict) -> dict | None:
    if path == POSTS:
        _writer(actor)
        return _call(services, actor, "post", _content(body))
    post_id, action = _split(path)
    if post_id and action == "replies":
        _writer(actor)
        return _call(services, actor, "reply", dict(_content(body), id=post_id))
    if post_id and action == "like":
        _writer(actor)
        return _call(services, actor, "like", {"id": post_id})
    return None


def delete(services, actor: Actor, path: str) -> dict | None:
    post_id, action = _split(path)
    if post_id and not action:
        _writer(actor)
        return _call(services, actor, "delete", {"id": post_id})
    return None


def owns(path: str) -> bool:
    """POST 路由先问这一句，免得不是公共区的请求也被读一次 body。"""
    return path == POSTS or path.startswith(POSTS + "/")


__all__ = ["delete", "get", "owns", "post"]
