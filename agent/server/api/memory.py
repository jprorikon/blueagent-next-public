"""記憶的唯讀檢視：提煉履歷（trace）與窗口摘要。

⚠️ **這兩支只給主人**（owner 2026-11-02「做掉」）：它們是她的記憶本身（窗口摘要＝她的理解、
事實履歷＝她記得的東西怎麼被改寫），跟 `/api/mood`（她的內心）、`/api/diary`（她的日記）同一口徑。
2026-11-02 之前這裡只檢查「有沒有登入」⇒ 任何 guest token 直接打都讀得到（App UI 沒入口，
但 API 沒有閘）。判據：`tests/test_guest_permissions.py`。
"""

from __future__ import annotations

from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext

from agent.server.api.common import _owner_required


def get_memory_trace(state: AppState, query: dict, auth: AuthContext | None = None
                     ) -> tuple[int, dict]:
    """`GET /api/memory/trace?q=文字` 或 `?fact_key=正規化文字`。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    text = str((query.get("fact_key") or [""])[0] or "").strip()
    if not text:
        raw = str((query.get("q") or [""])[0] or "").strip()
        if not raw:
            return 400, {"error": "need_q_or_fact_key"}
        from agent.brain.memory.facts import normalize_text
        text = normalize_text(raw)
    limit = int((query.get("limit") or ["20"])[0] or 20)
    rows = state.memory_views.trace(text, limit=limit)
    return 200, {"ok": True, "fact_key": text, "count": len(rows), "history": rows}


def get_memory_windows(state: AppState, query: dict, auth: AuthContext | None = None
                       ) -> tuple[int, dict]:
    """`GET /api/memory/windows?entity=mira&limit=5`（只給主人，見檔頭）。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity = str((query.get("entity") or ["mira"])[0] or "mira")
    limit = max(1, min(50, int((query.get("limit") or ["5"])[0] or 5)))
    rows = state.memory_views.windows(entity, limit=limit)
    return 200, {"ok": True, "entity": entity, "count": len(rows), "windows": rows}
