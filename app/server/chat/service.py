"""1:1 聊天用例：认证身份、参与者权限、输入规范化与幂等写入。"""

from __future__ import annotations

import time

from app.server.chat.store import ChatStore
from app.server.social.models import Actor, BoardError

_ALLOWED_PARTS = {"text", "photo", "sticker"}
AGENT_ROOM_ID = "cnv_agent_room"


def normalize_parts(text="", parts=None) -> list[dict]:
    if parts is None:
        parts = [{"kind": "text", "text": str(text or "")}]
    if not isinstance(parts, list) or not parts or len(parts) > 24:
        raise BoardError(400, "invalid_parts")
    result = []
    total = 0
    for raw in parts:
        if isinstance(raw, str):
            raw = {"kind": "text", "text": raw}
        if not isinstance(raw, dict):
            raise BoardError(400, "invalid_parts")
        kind = str(raw.get("kind") or "text")
        if kind not in _ALLOWED_PARTS:
            raise BoardError(400, "invalid_part_kind")
        part = {"kind": kind, "text": str(raw.get("text") or "").strip(),
                "media_id": str(raw.get("media_id") or ""),
                "sticker": str(raw.get("sticker") or ""),
                "url": str(raw.get("url") or "")}
        total += len(part["text"])
        if kind == "photo" and not (part["media_id"] or part["url"]):
            raise BoardError(400, "photo_required")
        if kind == "sticker" and not part["sticker"]:
            raise BoardError(400, "sticker_required")
        if kind == "text" and not part["text"]:
            raise BoardError(400, "empty_part")
        result.append(part)
    if total > 8000:
        raise BoardError(400, "text_too_long")
    return result


class ChatService:
    def __init__(self, store: ChatStore, accounts, *, clock=time.time, media_owner=None,
                 media_purge=None) -> None:
        self.store = store
        self.accounts = accounts
        self.clock = clock
        self.media_owner = media_owner
        #: `media_purge(media_id, owner_id) -> bool`：刪訊息後，沒人再引用的圖片連檔案一起刪
        self.media_purge = media_purge

    def open_direct(self, actor: Actor, other_id: str) -> dict:
        other = self.accounts.get(str(other_id or ""))
        if other is None or other.id == actor.id:
            raise BoardError(404, "account_not_found")
        if actor.kind != "entity" and other.kind != "entity" \
                and {actor.kind, other.kind} != {"owner", "guest"}:
            # C-09：新增人際 DM 只允許 owner↔guest；guest↔guest 不開放。
            raise BoardError(403, "direct_requires_owner")
        if actor.visible_entities is not None and other.kind == "entity" \
                and other.id not in actor.visible_entities:
            raise BoardError(403, "entity_not_visible")
        return self.store.ensure_direct(actor.id, other.id, self.clock())

    def record_missed_call(self, entity_id: str, owner_id: str, call_id: str) -> dict | None:
        """她打給主人、主人沒接（逾時／她響鈴中掛掉）：在兩人的私聊裡留一則「不在着信」（owner 2026-11-17）。

        以她的名義寫一個 `kind=call` 的部件（文字留空，客戶端畫成置中一行「不在着信」）——
        App 在背景或被關掉時，主人回來打開私聊就看得到她打過。這種部件只由平台自己寫，
        `normalize_parts` 不收（客戶端不能偽造）。同一通只寫一次（`client_mutation_id=missed-<call_id>`）。
        """
        entity = self.accounts.get(str(entity_id or ""))
        owner = self.accounts.get(str(owner_id or ""))
        if entity is None or owner is None or entity.kind != "entity":
            return None
        actor = entity.actor()
        conversation = self.store.ensure_direct(actor.id, owner.id, self.clock())
        conversation_id = str((conversation or {}).get("id") or "")
        if not conversation_id:
            return None
        part = {"kind": "call", "text": "", "media_id": "", "sticker": "", "url": ""}
        return self.store.send(actor=actor, conversation_id=conversation_id, parts=[part],
                               at=self.clock(), client_mutation_id=("missed-" + str(call_id))[:128])

    def open_agent_room(self, actor: Actor) -> dict:
        """C-10：唯一固定房；資料仍是通用 group／participant，不寫死成員私有流。"""
        if actor.kind not in {"owner", "entity"}:
            raise BoardError(403, "agent_room_forbidden")
        accounts = self.accounts.all()
        owners = [account for account in accounts if account.kind == "owner"]
        if len(owners) != 1:
            raise BoardError(503, "owner_unavailable")
        participants = [owners[0]] + sorted(
            (account for account in accounts if account.kind == "entity"),
            key=lambda account: account.id)
        if len(participants) < 2:
            raise BoardError(503, "agent_room_unavailable")
        self.store.ensure_group(
            AGENT_ROOM_ID, [account.id for account in participants],
            created_by=owners[0].id, at=self.clock())
        return self.store.conversation(actor.id, AGENT_ROOM_ID)

    def list(self, actor: Actor) -> dict:
        """每個會話附上成員的 id／kind／display（2026-11-08，客人私聊 P-C2）：客戶端靠它分「角色」與「人」，
        並給人與人之間的私聊畫名字與頭像；以前只有 participants 的 id，認不出客人。"""
        rows = self.store.list(actor.id)
        for row in rows:
            row["members"] = [self._member(account_id) for account_id in row.get("participants") or []]
        return {"conversations": rows}

    def _member(self, account_id: str) -> dict:
        account = self.accounts.get(str(account_id)) if self.accounts is not None else None
        if account is None:
            return {"id": str(account_id), "kind": "", "display": str(account_id)}
        return {"id": account.id, "kind": account.kind, "display": account.display}

    def messages(self, actor: Actor, conversation_id: str, *, before=0, after=0,
                 limit=50) -> dict:
        return self.store.messages(actor.id, conversation_id, before=before,
                                   after=after, limit=limit)

    def send(self, actor: Actor, conversation_id: str, *, text="", parts=None,
             client_mutation_id="", causation_id="") -> dict:
        normalized = normalize_parts(text, parts)
        for part in normalized:
            if part["kind"] != "photo":
                continue
            # 新消息只接受本平台已上传且属于发送者的媒体；legacy 导入不经过这里。
            if self.media_owner is not None and (
                    not part["media_id"] or part["url"]
                    or self.media_owner(part["media_id"]) != actor.id):
                raise BoardError(400, "unknown_media")
        return self.store.send(
            actor=actor, conversation_id=str(conversation_id or ""), parts=normalized,
            at=self.clock(), client_mutation_id=str(client_mutation_id or "")[:128],
            causation_id=str(causation_id or "")[:128])

    def delete(self, actor: Actor, conversation_id: str, message_id: str) -> dict:
        """刪自己發的一則（`DELETE /v1/conversations/{id}/messages/{message_id}`）。

        訊息留墓碑；它帶的圖片若是自己上傳的、而且別處（其他訊息、公共區）都沒再用，連檔案一起刪。
        """
        tombstone, media = self.store.delete_message(
            actor=actor, conversation_id=str(conversation_id or ""),
            message_id=str(message_id or ""), at=self.clock())
        purged = []
        if self.media_purge is not None:
            purged = [media_id for media_id in media if self.media_purge(media_id, actor.id)]
        return {"message": tombstone, "media_deleted": purged}

    def mark_read(self, actor: Actor, conversation_id: str, message_id: str) -> dict:
        return self.store.mark_read(actor.id, str(conversation_id or ""),
                                    str(message_id or ""), self.clock(),
                                    actor_kind=str(getattr(actor, "kind", "") or ""))


__all__ = ["AGENT_ROOM_ID", "ChatService", "normalize_parts"]
