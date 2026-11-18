"""旧公共区 API／stdio MCP 的远程兼容壳；凭证按调用者解析，绝不共享改写。"""

from __future__ import annotations

import json

from agent.adapter.app_protocol import Actor, BoardError
from agent.adapter.app_server import AppServerClient


class RemoteBoardService:
    """与 BoardService 相同的窄方法面，但所有真值都在 App Server。"""

    def __init__(self, base_url: str, credential, *, timeout: float = 15.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.credential = credential
        self.timeout = float(timeout)

    def _token(self, actor: Actor) -> str:
        token = self.credential(actor) if callable(self.credential) else self.credential
        token = str(token or "")
        if not token:
            raise BoardError(503, "app_public_token_missing")
        return token

    def _call(self, actor: Actor, name: str, arguments: dict) -> dict:
        try:
            client = AppServerClient(self.base_url, self._token(actor), timeout=self.timeout)
            return client.board_call(name, arguments)
        except BoardError:
            raise
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BoardError(503, "app_server_unavailable") from exc

    def feed(self, actor: Actor, *, before: str = "", limit: int = 20,
             since: float = 0.0, authors: set[str] | None = None) -> dict:
        return self._call(actor, "feed", {
            "before": str(before or ""), "limit": int(limit or 20), "since": float(since or 0),
            "authors": sorted(authors or []),
        })

    def get(self, actor: Actor, post_id: str) -> dict:
        return self._call(actor, "get", {"id": str(post_id)})

    def post(self, actor: Actor, body, media=(), client_mutation_id: str = "", *,
             media_owner=None, source_ref: str = "") -> dict:
        return self._call(actor, "post", {
            "text": body, "media": list(media or []),
            "client_mutation_id": str(client_mutation_id or ""),
            "source_ref": str(source_ref or ""),
        })

    def reply(self, actor: Actor, post_id: str, body, media=(), client_mutation_id: str = "", *,
              media_owner=None) -> dict:
        return self._call(actor, "reply", {
            "id": str(post_id), "text": body, "media": list(media or []),
            "client_mutation_id": str(client_mutation_id or ""),
        })

    def like(self, actor: Actor, post_id: str) -> dict:
        return self._call(actor, "like", {"id": str(post_id)})

    def delete(self, actor: Actor, post_id: str) -> dict:
        return self._call(actor, "delete", {"id": str(post_id)})

    def can_read_media(self, actor: Actor, media_id: str, owner_principal: str = "") -> bool:
        try:
            self._call(actor, "media.get", {"id": str(media_id)})
            return True
        except BoardError:
            return False

    def list_moods(self, actor: Actor) -> dict:
        return self._call(actor, "mood.list", {})

    def set_mood(self, actor: Actor, key, note="") -> dict:
        return self._call(actor, "mood.set", {"key": key, "note": note})

    def close(self) -> None:
        return None


__all__ = ["RemoteBoardService"]
