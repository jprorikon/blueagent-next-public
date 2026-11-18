"""`POST /api/app/kick`：App Server 有新事件時叫醒她的 App 適配器（規格 `docs/PLAN-app-kick-20261108.md`）。

為什麼是這個形狀（照 `server/http.py` 的 `/trpg/kick` 現役做法，不另發明）：

  · 平台只會 **kick**（帶 `X-App-Kick-Token` 驗身分），內容一律叫她自己去拉 `/v1/events`；
  · **kick 只是提示**，重複幾次都無害——真正推進游標的是拉取，且游標在事件完整處理後才落盤；
  · 只叫名單裡的實體（一個端點、按 entity 過濾，owner 2026-11-08 拍板）。

回 `{"ok": True, "woken": [...], "unknown": [...]}`：未知實體不報錯（平台不該為了她改名而失敗），
只列出來給日誌與測試看。
"""

from __future__ import annotations

import hmac
import os


def token() -> str:
    """現役 kick token（留空＝端點未啟用，回 503）。"""
    return str(os.environ.get("BLUEAGENT_APP_KICK_TOKEN") or "").strip()


def kick(state, entities) -> dict:
    """叫醒名單裡的實體；回叫醒了誰、誰不認得。"""
    adapters = {str(getattr(item, "entity", "")): item
                for item in (getattr(state, "app_chat", None) or [])}
    woken: list[str] = []
    unknown: list[str] = []
    for entity in dict.fromkeys(str(item) for item in (entities or [])):
        if not entity:
            continue
        adapter = adapters.get(entity)
        if adapter is None:
            unknown.append(entity)
            continue
        adapter.wake()
        woken.append(entity)
    return {"ok": True, "woken": woken, "unknown": unknown}


__all__ = ["kick", "token"]


def handle(state, given: str | None, body, path: str = "/api/app/kick") -> tuple[int, dict]:
    """`POST /api/app/kick` 整支：`X-App-Kick-Token` 驗身分 ⇒ `kick()`。

    **同步處理、立刻回**：kick 只是把 `poll_once` 的等待叫停，真正的回合在適配器自己的執行緒裡跑，
    所以不會像 `/trpg/kick` 那樣需要先回 200 再跑。重複 kick 無害（游標在事件處理完才推進）。
    """
    expect = token()
    if not expect:
        return 503, {"ok": False, "error": "kick_token_not_configured"}
    got = str(given or "")
    if not got or not hmac.compare_digest(got, expect):
        return 403, {"ok": False, "error": "bad token"}
    if path == "/api/app/introspect":
        # App Server 問「這把 token 是誰的哪一台」（多設備配對，2026-11-16）：同一把內部 token
        from agent.server.api.pairing import introspect
        return introspect(state, body or {})
    return 200, kick(state, (body or {}).get("entities") or [])
