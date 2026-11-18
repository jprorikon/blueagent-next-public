"""主人的控制面：活動、語音、封片、立場、指令佇列、手動主動消息。"""

from __future__ import annotations

from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext, Role
from agent.brain.domain.client_end import end_of

from agent.server.api.common import _id, _owner_required, _participants


def post_activity(state: AppState, action: str, body: dict,
                  auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    if action == "start":
        entity, principal = _id(body)
        return 200, state.start_activity(entity=entity, principal=principal)
    if action == "pause":
        return 200, state.pause_activity()
    if action == "resume":
        return 200, state.resume_activity()
    if action == "reset":
        return 200, state.reset_activity()
    if action == "reload":
        return 200, state.reload_activity()
    return 404, {"error": "not found"}


def post_voice(state: AppState, action: str, body: dict | None = None,
               auth: AuthContext | None = None, source: str = "") -> tuple[int, dict]:
    """`/api/voice/<call|ping|interrupt|hangup>`：App 的單人通話（只給主人，owner 2026-10-29）。

    `source`＝請求的 `X-Source`：哪一端撥的（手機／MR，`domain/client_end.py`）；後撥的蓋掉先撥的。
    """
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    body = dict(body or {})
    entity, _ = _id(body)
    if state.app.entities.get(entity) is None:
        return 400, {"error": f"unknown entity: {entity}"}
    principal = getattr(auth, "principal", "") or "owner"
    if action == "call":
        return state.start_call(entity, principal, end_of(source))
    if action == "ping":
        return state.ping_call(entity, str(body.get("call_id") or ""))
    if action == "interrupt":
        return state.interrupt_call(entity, principal)
    if action == "hangup":
        data = state.hangup_voice(entity, str(body.get("call_id") or ""))
        if data.get("error") == "call_ended":
            return 410, data
        return 200, data
    return 404, {"error": "not found"}


def post_seal(state: AppState, body: dict,
              auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity, principal = _id(body)
    return 200, state.seal(entity=entity, principal=principal,
                           group=bool(body.get("group")),
                           participants=_participants(body),
                           ownerless=bool(body.get("ownerless")))


def get_owner_stances(state: AppState, query: dict,
                      auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity = (query.get("entity") or ["mira"])[0]
    try:
        return 200, state.owner_stances(entity)
    except KeyError as exc:
        return 400, {"error": f"unknown id: {exc}"}


def post_owner_stance(state: AppState, body: dict,
                      auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity, principal = _id(body)
    weights = body.get("weights")
    notes = body.get("notes")
    if weights is not None and not isinstance(weights, dict):
        return 400, {"error": "weights must be an object"}
    if notes is not None and not isinstance(notes, dict):
        return 400, {"error": "notes must be an object"}
    trust = body.get("trust")
    if trust is not None:
        try:
            trust = float(trust)
        except (TypeError, ValueError):
            return 400, {"error": "trust must be a number"}
    try:
        return 200, state.set_stance(entity=entity, principal=principal,
                                     weights=weights, notes=notes, trust=trust)
    except KeyError as exc:
        return 400, {"error": f"unknown id: {exc}"}
    except ValueError as exc:
        return 400, {"error": str(exc)}


def get_commands(state: AppState, query: dict,
                 auth: AuthContext | None = None) -> tuple[int, dict]:
    """主人指令佇列的狀態：排到第幾條、哪條在跑、最近幾條的結果。只有主人能看。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    queue = getattr(state, "commands", None)
    if queue is None:
        return 503, {"error": "command_queue_unavailable"}
    try:
        limit = int((query.get("limit") or ["20"])[0])
    except (TypeError, ValueError):
        limit = 20
    return 200, {"ok": True, "commands": queue.snapshot(limit=limit)}


def post_command(state: AppState, body: dict,
                 auth: AuthContext | None = None) -> tuple[int, dict]:
    """`POST /api/command`：把一句話當**工作指令**排隊（不是閒聊）。

    與 `/api/turn` 的差別：這裡立刻回 `202 + command_id`，做完的結果從
    `GET /api/commands` 查或（語音那條）由 `GET /api/events` 推 `delivery`。
    """
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    queue = getattr(state, "commands", None)
    if queue is None:
        return 503, {"error": "command_queue_unavailable"}
    text = str(body.get("text") or "").strip()
    if not text:
        return 400, {"error": "text_required"}
    entity = str(body.get("entity") or "mira").strip() or "mira"
    submitted = queue.submit(text, entity=entity, principal="owner",
                             source=str(body.get("source") or "api:command"),
                             kind=str(body.get("kind") or "work"),
                             meta={"mode_note": str(body.get("note") or "")}
                             if body.get("note") else {})
    if not submitted.get("ok"):
        reason = submitted.get("reason")
        # kind：trpg／game／read 收得下但還沒實作（owner 2026-11-16：這輪只做骨架）⇒ 501；不認得 ⇒ 400
        status = {"kind_not_implemented": 501, "kind_unknown": 400, "empty": 400}.get(reason, 429)
        return status, {"ok": False, "error": reason, "pending": submitted.get("pending"),
                        "open_for_entity": submitted.get("open_for_entity")}
    return 202, {"ok": True, "command_id": submitted.get("id"),
                 "queued": submitted.get("queued"), "entity": entity}


def post_command_cancel(state: AppState, body: dict,
                        auth: AuthContext | None = None) -> tuple[int, dict]:
    """`POST /api/command/cancel` `{"id": 3}`：取消一條指令（步邊界停）。

    還在排隊 ⇒ 立刻 `cancelled`；正在跑 ⇒ 不殺，那一步跑完就停、結果不交付（回 `cancel_requested`）；
    已經結束 ⇒ 409；找不到 ⇒ 404。
    """
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    queue = getattr(state, "commands", None)
    if queue is None:
        return 503, {"error": "command_queue_unavailable"}
    outcome = queue.cancel(body.get("id"))
    if outcome.get("ok"):
        return 200, outcome
    return (409 if outcome.get("reason") == "already_finished" else 404), outcome


def post_owner_proactive(state: AppState, body: dict,
                         auth: AuthContext | None = None) -> tuple[int, dict]:
    """主人手動叫她主動說一句（`POST /api/owner/proactive`）。

    body: `{"entity": "mira", "text": "…"?}`；不給 `text` 就讓她**自己組織**一句（跑一次心跳）。
    這條會**跳過暖機與最小間隔**（`force`），但仍然走去重與安靜時段的禮貌——主人自己叫的，
    不該被「剛重啟」擋掉。
    """
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity = str(body.get("entity") or "mira").strip()
    text = str(body.get("text") or "").strip()
    if text:
        gate = getattr(state.app, "proactive", None)
        publish = getattr(state.app, "delivery", None)
        if gate is None:
            return 503, {"error": "proactive_unavailable"}
        outcome = gate.maybe_send(entity, text, principal="owner",
                                  publish=(publish.send if publish is not None else None),
                                  force=True)
        return (200 if outcome.get("sent") else 409), {"ok": bool(outcome.get("sent")),
                                                       **outcome}
    beats = getattr(state.app, "heartbeats", None) or getattr(state.app, "heartbeat", None)
    beat = beats.get(entity) if beats is not None and hasattr(beats, "get") else None
    if beat is None:
        return 404, {"error": "entity_not_found", "entity": entity}
    frames = beat.tick(force=True)
    return (200 if frames else 409), {"ok": bool(frames), "entity": entity,
                                      "frames": frames,
                                      "last_skip": getattr(beat, "last_skip_reason", "")}
