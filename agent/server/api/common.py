"""共用：401／403、owner 閘、請求體裡的 id／參與者。"""

from __future__ import annotations

from agent.brain.domain.auth import AuthContext, Role


def _unauthorized() -> tuple[int, dict]:
    return 401, {"error": "unauthorized",
                 "hint": "Authorization: Bearer <owner-or-guest-token>"}


def _forbidden() -> tuple[int, dict]:
    return 403, {"error": "forbidden"}


def _owner_required(auth: AuthContext | None) -> tuple[int, dict] | None:
    if auth is None:
        return _unauthorized()
    if not auth.is_owner:
        return _forbidden()
    return None


def _id(body: dict) -> tuple[str, str]:
    return (str(body.get("entity") or "mira").strip(),
            str(body.get("principal") or "owner").strip())


def _participants(body: dict):
    people = body.get("participants")
    return people if isinstance(people, list) else None
