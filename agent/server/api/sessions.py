"""會話列表：只讀聚合現有 entity 與 outbox，不建立第二份聊天真值。"""

from __future__ import annotations

from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext

from agent.server.api.common import _forbidden, _unauthorized


def list_sessions(state: AppState, query: dict, *, auth: AuthContext | None = None
                  ) -> tuple[int, dict]:
    """`GET /api/sessions`：目前帳號看得見的 agent 1:1，各回最新一輪。

    未讀是端側狀態（M0 `ConversationStore`），服務端不假裝知道哪一則已被使用者看見；
    回包中的 `unread=0` 只是形狀佔位，Unity 必須以自己的未讀窗口覆蓋它。
    `guests`／`rooms` 在 P-C1 固定為空，後續分期各自接自己的存儲。
    """
    del query
    if auth is None:
        return _unauthorized()
    if not auth.can("chat"):
        return _forbidden()
    outbox = getattr(state.app, "outbox", None)
    if outbox is None:
        return 503, {"error": "outbox_unavailable"}
    try:
        found = state.app.entities.all()
        if auth.is_owner:
            visible = list(found)
        else:
            bound = [item for item in found if item.id == auth.principal]
            visible = bound or list(found)
    except Exception:  # noqa: BLE001 - 與 `/api/me` 同一個失敗邊界
        visible = []

    # 與聊天歷史同一個來源邊界：只看 App 端，不把 Discord、MR 或通話內容混進列表。
    rows = outbox.since("", principal=auth.principal, limit=400)
    rows = [row for row in rows
            if str(row.get("kind") or "") != "call"
            and str(row.get("source") or "").startswith(("phone", "api"))]
    latest = {}
    for row in rows:
        entity = str(row.get("entity") or "")
        if entity:
            latest[entity] = row                  # outbox 依接收順序，最後一列就是最新活動

    agents = []
    for item in visible:
        row = latest.get(item.id) or {}
        asked = str(row.get("asked") or "").strip()
        text = str(row.get("text") or "").strip() or asked
        agents.append({
            "entity": item.id,
            "name": item.name,
            "last": text,
            "at": float(row.get("at") or 0.0),
            "turn_id": str(row.get("turn_id") or ""),
            "unread": 0,
        })
    # 同時間沒有歷史的角色保持 `/api/me` 的順序；有活動者最新在前。
    agents.sort(key=lambda row: row["at"], reverse=True)
    return 200, {"agents": agents, "guests": [], "rooms": []}


__all__ = ["list_sessions"]
