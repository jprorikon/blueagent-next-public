"""TRPG 桌與小遊戲：/api/trpg/*、/api/game、/api/self/next。"""

from __future__ import annotations

from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext

from agent.server.api.common import _owner_required


def _principal(auth: AuthContext | None) -> str:
    return getattr(auth, "principal", "") or ""


def _table_owner(auth: AuthContext | None) -> tuple[int, dict] | None:
    """TRPG 整組 API **只給主人**（owner 2026-10-29「不要讓 guest 控制 agent 加入 trpg」）。

    看桌列表／一桌的摘要也擋：App 裡 guest 本來就進不了 TRPG 頁，這兩支對 guest 沒有用途；
    而且它們是拿**她的** hub 帳號去問的 ⇒ 開給 guest 等於借她的身分看桌。
    """
    return _owner_required(auth if _principal(auth) else None)


def get_trpg_tables(state: AppState, query: dict, *, auth: AuthContext | None = None) -> tuple[int, dict]:
    """`GET /api/trpg/tables?entity=`：桌列表（用那個角色的 account token 去問 hub）。"""
    refused = _table_owner(auth)
    if refused is not None:
        return refused
    entity = str((query.get("entity") or ["mira"])[0] or "mira")
    try:
        tables = state.trpg_service.tables(entity)
        has_token = state.trpg_service.token_available(entity)
    except Exception as exc:  # noqa: BLE001 - 連不上就 502，不要 500
        return 502, {"error": "trpg_unavailable", "detail": str(exc)}
    return 200, {"entity": entity, "has_token": has_token, "tables": tables}


def get_trpg_table(state: AppState, query: dict, *, auth: AuthContext | None = None) -> tuple[int, dict]:
    """`GET /api/trpg/table?id=&entity=`：一桌的摘要（牌面另外用 view 取）。"""
    refused = _table_owner(auth)
    if refused is not None:
        return refused
    entity = str((query.get("entity") or ["mira"])[0] or "mira")
    table_id = str((query.get("id") or [""])[0] or "").strip()
    if not table_id:
        return 400, {"error": "id required"}
    try:
        summary = state.trpg_service.table(entity, table_id)
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": "trpg_unavailable", "detail": str(exc)}
    if not summary:
        return 404, {"error": "table_not_found"}
    return 200, {"entity": entity, "table": summary}


def post_trpg_say(state: AppState, body: dict, auth: AuthContext | None = None) -> tuple[int, dict]:
    """`POST /api/trpg/say` `{table, text, entity?, seat?}`：場外發話。"""
    refused = _table_owner(auth)
    if refused is not None:
        return refused
    table_id = str(body.get("table") or "").strip()
    text = str(body.get("text") or "").strip()
    if not table_id or not text:
        return 400, {"error": "table and text required"}
    entity = str(body.get("entity") or "mira").strip() or "mira"
    try:
        out = state.trpg_service.say(entity, table_id, text, seat=str(body.get("seat") or ""),
                                    session=str(body.get("session") or ""))
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": "trpg_unavailable", "detail": str(exc)}
    return 200, {"ok": True, "say": out}


def get_self_task(state: AppState, body: dict,
                  auth: AuthContext | None = None) -> tuple[int, dict]:
    """`GET /api/self/next?entity=`：回 AEM 的自主任務決策（**只給主人**，見檔頭）。

    ⚠️ 2026-11-02 之前只檢查「有沒有登入」⇒ guest token 打得到她的自主任務決策；
    App 沒有這個入口，但 API 不該開著。
    """
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    raw_entity = body.get("entity")
    if isinstance(raw_entity, (list, tuple)):
        raw_entity = raw_entity[0] if raw_entity else ""
    entity = str(raw_entity or "mira").strip() or "mira"
    return 200, state.inner.self_task(entity)


def post_trpg_enter(state: AppState, body: dict,
                    auth: AuthContext | None = None) -> tuple[int, dict]:
    """`POST /api/trpg/enter`：選角色進桌；她拒絕時保留原對話頁。"""
    refused = _table_owner(auth)
    if refused is not None:
        return refused
    table_id = str(body.get("table") or "").strip()
    if not table_id:
        return 400, {"error": "table required"}
    entity = str(body.get("entity") or "mira").strip() or "mira"
    seat_obj = getattr(getattr(state, "app", None), "trpg", None)
    ask = getattr(seat_obj, "seat_state", None)
    if callable(ask):
        try:
            state_now = ask() or {}
        except Exception as exc:  # noqa: BLE001
            print(f"  trpg: seat_state 失敗（{type(exc).__name__}: {exc}）", flush=True)
            state_now = {}
        print(f"  trpg: enter 前置檢查 seat={type(seat_obj).__name__} "
              f"state={ {k: state_now.get(k) for k in ('declined', 'valence', 'last_line')} }", flush=True)
        if state_now.get("declined"):
            return 200, {"ok": True, "declined": True, "entity": entity, "table": table_id,
                         "line": str(state_now.get("last_line") or ""),
                         "valence": state_now.get("valence"),
                         "reason": "she_does_not_want_to_play"}
    try:
        out = state.trpg_service.enter(entity, table_id, char_id=str(body.get("char") or ""))
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": "trpg_unavailable", "detail": str(exc)}
    return 200, {"ok": True, "entity": entity, "enter": out}


def post_trpg_act(state: AppState, body: dict, auth: AuthContext | None = None) -> tuple[int, dict]:
    """`POST /api/trpg/act`：她在桌上的一個行動（數值由引擎裁決）。"""
    refused = _table_owner(auth)
    if refused is not None:
        return refused
    table_id = str(body.get("table") or "").strip()
    action = str(body.get("action") or "").strip()
    if not table_id or not action:
        return 400, {"error": "table and action required"}
    entity = str(body.get("entity") or "mira").strip() or "mira"
    try:
        out = state.trpg_service.act(entity, table_id, action, seat=str(body.get("seat") or ""),
                                    session=str(body.get("session") or ""),
                                    target=str(body.get("target") or ""),
                                    attr=str(body.get("attr") or ""), dc=int(body.get("dc") or 0),
                                    note=str(body.get("note") or ""))
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": "trpg_unavailable", "detail": str(exc)}
    return 200, {"ok": True, "act": out}


def post_game(state: AppState, body: dict,
              auth: AuthContext | None = None) -> tuple[int, dict]:
    """`POST /api/game`：五子棋（`start`／`move`／`frame`／`close`）。"""
    principal = _principal(auth)
    if not principal:
        return 401, {"error": "unauthorized"}
    store = getattr(state, "games", None)
    if store is None:
        return 503, {"error": "game_unavailable"}
    op = str(body.get("op") or "").strip().lower()
    gid = str(body.get("game") or "").strip()
    entity = str(body.get("entity") or "mira").strip() or "mira"
    def phase(text: str) -> None:
        events = getattr(getattr(state, "app", None), "world_events", None)
        if events is not None:
            events.publish("game_phase", text, entities=(entity,), principals=(principal,))

    try:
        if op == "start":
            frame = store.start(principal=principal, entity=entity,
                                difficulty=str(body.get("difficulty") or "normal"))
            phase("五目並べが始まった")
            return 200, {"ok": True, "game": frame}
        if op == "move":
            frame = store.move(principal=principal, gid=gid, x=int(body.get("x")), y=int(body.get("y")))
            if frame.get("winner") and frame.get("note") != "already_over":
                phase("五目並べの勝敗が決まった")
            return 200, {"ok": True, "game": frame, "note": frame.get("note", "")}
        if op == "frame":
            return 200, {"ok": True, "game": store.frame(principal=principal, gid=gid)}
        if op == "close":
            result = store.close(principal=principal, gid=gid)
            phase("五目並べを閉じた")
            return 200, result
    except KeyError as exc:
        return 400, {"ok": False, "error": f"missing field: {exc}"}
    except ValueError:
        return 400, {"ok": False, "error": "x/y must be int"}
    except Exception as exc:  # noqa: BLE001 - GameError 之類
        if type(exc).__name__ == "GameError":
            return 200, {"ok": False, "error": str(exc), "game": None}
        raise
    return 400, {"ok": False, "error": "unknown op"}
