"""訪客：加入、狀態、目錄、owner 審批、Discord 白名單。"""

from __future__ import annotations

from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext, Role

from agent.server.api.common import _owner_required


def get_guest_status(state: AppState, query: dict) -> tuple[int, dict]:
    secret = (query.get("secret") or [""])[0]
    return 200, state.guest_status(secret)


def get_guest_directory(state: AppState, query: dict,
                       auth: AuthContext | None = None) -> tuple[int, dict]:
    """加入前需要看到已有 guest；用邀请码或已签发的 token 换这份名单。"""
    if auth is None:
        code = (query.get("invite_code") or [""])[0]
        if not state.app.guest_store.has_invite(code):
            return 403, {"error": "invite_required"}
    return 200, state.guest_directory()


def get_owner_guests(state: AppState,
                     auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    return 200, state.owner_guests()


def get_whitelist(state: AppState,
                  auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    return 200, {"ok": True, "whitelist": state.app.guest_store.whitelist()}


def post_guest_join(state: AppState, body: dict, ip: str = "") -> tuple[int, dict]:
    if not state.app.auth.allow_join(ip):
        return 429, {"error": "rate_limited", "retry_after_s": 60}
    declared = body.get("declared")
    if not isinstance(declared, dict):
        declared = {}
    try:
        payload = state.join_guest(
            invite_code=str(body.get("invite_code") or ""),
            discord_id=str(body.get("discord_id") or ""),
            display_name=str(body.get("display_name") or ""),
            declared_kind=str(declared.get("kind") or body.get("declared_kind") or "other"),
            declared_label=str(declared.get("label") or body.get("declared_label") or ""),
            declared_note=str(declared.get("note") or body.get("declared_note") or ""),
            guest_relations=body.get("guest_relations") or ())
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, payload


def post_guest_action(state: AppState, path: str, body: dict,
                      auth: AuthContext | None = None) -> tuple[int, dict]:
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    parts = [part for part in path.strip("/").split("/") if part]
    # api / owner / guests / <request_id> / <action>
    if len(parts) != 5:
        return 404, {"error": "not found"}
    request_id, action = parts[3], parts[4]
    try:
        if action == "approve":
            return 200, state.approve_guest(
                request_id,
                final_kind=str(body.get("final_kind") or ""),
                final_label=str(body.get("final_label") or ""),
                trust=float(body.get("trust") or 0.5),
                capabilities=body.get("capabilities") or ("chat",),
                final_guest_relations=body.get("guest_relations") or ())
        if action == "reject":
            return 200, state.reject_guest(request_id)
        if action == "revoke":
            return 200, state.revoke_guest(request_id)
    except (KeyError, ValueError) as exc:
        return 400, {"error": str(exc)}
    return 404, {"error": "not found"}
