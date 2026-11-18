"""旧 `/api/media/<id>` 的公共区授权判定：这张图能不能给这个调用者看，问 App Server。

公共区本身的读写（`/api/feed`、`/api/post…`）已于 2026-11-08 从 Agent 移除，客户端直连
App Server 的 `/v1/feed`、`/v1/posts`（`app/server/board_http.py`）。这里只剩旧消息里
`/api/media/` 链接的授权：图属于公开帖时，其他读者也能看；判定仍以 App Server 为准。
"""

from __future__ import annotations

from agent.adapter.app_protocol import Actor, BoardError
from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext


def _actor(state: AppState, auth: AuthContext | None) -> Actor:
    if auth is None:
        raise BoardError(401, "unauthorized", hint="Authorization: Bearer <owner-or-guest-token>")
    if not auth.can("chat"):
        raise BoardError(403, "forbidden")
    found = state.app.entities.all()
    visible = None
    if not auth.is_owner:
        bound = [item.id for item in found if item.id == auth.principal]
        if bound:
            visible = frozenset(bound)
    kind = "owner" if auth.is_owner else "guest"
    people = [*state.app.principals.all(), *found]
    display = next((item.name for item in people if item.id == auth.principal), auth.principal)
    return Actor(auth.principal, kind, display, visible)


def can_read_media(state: AppState, media_id: str, owner_principal: str, *,
                   auth: AuthContext | None = None) -> bool:
    try:
        return state.board_for(auth).can_read_media(_actor(state, auth), media_id, owner_principal)
    except BoardError:
        return False


__all__ = ["can_read_media"]
