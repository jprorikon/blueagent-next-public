"""避免主 HTTP 文件膨胀的薄路由；不承载业务规则。"""

from __future__ import annotations

from agent.server import api


def media(handler, media_id: str, query: dict) -> None:
    """媒體傳輸薄殼；是否可讀由 BoardService 判定。"""
    store = getattr(handler._state(), "media", None)
    target = store.path_of(media_id) if store is not None else None
    row = store.row_of(media_id) if store is not None else None
    auth = handler._auth()
    principal = getattr(auth, "principal", "") or ""
    owner = str((row or {}).get("principal") or "")
    allowed = principal and (not owner or api.can_read_media(
        handler._state(), media_id, owner, auth=auth))
    if target is None or row is None or not allowed:
        handler._json(404, {"error": "media_not_found"})
        return
    from agent.server.http_responses import mime_for
    handler._picture(target, mime_for(target), query)


def get(handler, path: str, query: dict) -> bool:
    # 腦端管理頁（owner 2026-11-09）：頁面本身公開（只是靜態檔），資料 API 一律要主人 token
    if path in ("/admin", "/admin/"):
        from agent.server.http import CLIENT
        handler._file(CLIENT / "admin.html")
        return True
    if path == "/api/admin/thoughts":
        from agent.server.api.admin import get_thoughts
        handler._json(*get_thoughts(handler._state(), query, auth=handler._auth()))
        return True
    if path == "/api/admin/aem":
        from agent.server.api.admin import get_aem
        handler._json(*get_aem(handler._state(), query, auth=handler._auth()))
        return True
    if path == "/api/admin/identities":
        from agent.server.api.admin import get_identities
        handler._json(*get_identities(handler._state(), auth=handler._auth()))
        return True
    if path == "/api/devices" or path.startswith("/pair/"):
        # 多設備配對（owner 2026-11-16）：端末清單要登入；落地頁公開（不消耗碼）
        from agent.server.api import pairing
        if path == "/api/devices":
            handler._json(*pairing.get_devices(handler._state(), auth=handler._auth()))
        else:
            status, page = pairing.pair_page(handler._state(), path[len("/pair/"):])
            data = page.encode("utf-8")
            handler.send_response(status)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(data)
        return True
    if path == "/api/sessions":
        handler._json(*api.list_sessions(handler._state(), query, auth=handler._auth()))
        return True
    # 公共区的 `/api/feed`／`/api/post` 已于 2026-11-08 移除：客户端直连 App Server `/v1/feed`、`/v1/posts`
    # （`app/server/board_http.py`），Agent 只经 MCP 碰公共区。
    return False


def delete(handler, path: str) -> None:
    handler._json(404, {"error": "not found"})


__all__ = ["delete", "get", "media"]
