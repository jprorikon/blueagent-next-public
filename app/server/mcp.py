"""App Server 远程 MCP：公共区、聊天、媒体与公开心情，身份只取认证账号。"""

from __future__ import annotations

import json

from app.server.social.models import Actor, BoardError
from app.server.social.service import BoardService

_PROPS = {
    "id": {"type": "string", "description": "投稿 ID"},
    "text": {"type": "string", "description": "公开正文（最多 2000 字）"},
    "media": {"type": "array", "items": {"type": "string"},
              "description": "既有 media_id，最多 4 件"},
    "client_mutation_id": {"type": "string", "description": "客户端幂等键；重试必须沿用同一个值"},
    "source_ref": {"type": "string", "description": "发布源的稳定引用；同账号同内容重复发布幂等"},
    "occurred_at": {"type": "number",
                    "description": "事情实际发生的时刻（unix 秒）。只有带 source_ref 的角色帖可用；"
                                   "省略＝现在。不能晚于现在、不能早于回看窗口（7 天）"},
}
TOOLS = [
    {"name": "feed", "description": "公共区を新しい順で読む。before と limit でページング。",
     "inputSchema": {"type": "object", "properties": {
         "before": {"type": "string"}, "limit": {"type": "integer"}, "since": {"type": "number"},
         "author": {"type": "string", "description": "作者 id（省略＝全部）"},
         "authors": {"type": "array", "items": {"type": "string"},
                     "description": "兼容旧 HTTP 的多作者过滤"}}}},
    {"name": "get", "description": "一つの投稿全文と一階層の公開返信を読む。",
     "inputSchema": {"type": "object", "properties": {"id": _PROPS["id"]}, "required": ["id"]}},
    {"name": "post", "description": "一言だけ公共区に投稿する。公開したい内容だけを送る。",
     "inputSchema": {"type": "object", "properties": {
        "text": _PROPS["text"], "media": _PROPS["media"],
        "client_mutation_id": _PROPS["client_mutation_id"],
        "source_ref": _PROPS["source_ref"], "occurred_at": _PROPS["occurred_at"]},
        "required": ["text"]}},
    {"name": "reply", "description": "他人の公開投稿へ一階層だけ返信する。私聊には使わない。",
     "inputSchema": {"type": "object", "properties": {
         "id": _PROPS["id"], "text": _PROPS["text"], "media": _PROPS["media"],
         "client_mutation_id": _PROPS["client_mutation_id"]},
         "required": ["id", "text"]}},
    {"name": "like", "description": "他人の投稿へいいね（同じ投稿へ二度押しても一件のまま）。",
     "inputSchema": {"type": "object", "properties": {"id": _PROPS["id"]}, "required": ["id"]}},
    {"name": "delete", "description": "自分の投稿を論理削除。他人の削除は主人だけ。",
     "inputSchema": {"type": "object", "properties": {"id": _PROPS["id"]}, "required": ["id"]}},
]

CHAT_TOOLS = [
    {"name": "chat.conversation.open", "description": "自分と指定アカウントの 1:1 会話を開く。",
     "inputSchema": {"type": "object", "properties": {
         "account_id": {"type": "string"}}, "required": ["account_id"]}},
    {"name": "chat.agent_room.open", "description": "主人と全 Agent が参加する固定ルームを開く。ゲストは使えない。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "chat.conversations", "description": "自分が参加する会話と未読数を読む。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "chat.messages", "description": "参加している会話のメッセージを読む。",
     "inputSchema": {"type": "object", "properties": {
         "conversation_id": {"type": "string"}, "before": {"type": "string"},
         "after": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["conversation_id"]}},
    {"name": "chat.message.send", "description": "会話へ確定メッセージを送る。進行中の delta は送らない。",
     "inputSchema": {"type": "object", "properties": {
         "conversation_id": {"type": "string"}, "text": {"type": "string"},
         "parts": {"type": "array", "items": {"type": "object"}},
         "client_mutation_id": _PROPS["client_mutation_id"],
         "causation_id": {"type": "string", "description": "返信元の event_id"}},
         "required": ["conversation_id"]}},
]


MEDIA_TOOLS = [
    {"name": "media.get", "description": "自分が見える画像のメタデータと認証付き取得 URL を読む。",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "media_id"}}, "required": ["id"]}},
]

STICKER_TOOLS = [
    {"name": "sticker.list",
     "description": "クライアントが再生するスタンプの id・意味・認証付き URL を読む。",
     "inputSchema": {"type": "object", "properties": {
         "entity": {"type": "string", "description": "省略時は自分（owner は mira）"}}}},
]

MOOD_TOOLS = [
    {"name": "mood.list", "description": "自分に見えるアカウントの公開気分を読む。私有 affect ではない。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mood.set", "description": "自分の公開気分を明示的に設定する。私有 affect は自動公開されない。",
     "inputSchema": {"type": "object", "properties": {
         "key": {"type": "string", "enum": ["calm", "happy", "excited", "irritated", "down", "lonely"]},
         "note": {"type": "string", "description": "公開する一言（任意、80 字まで）"}},
         "required": ["key"]}},
]

LOCATION_TOOLS = [
    {"name": "location.get",
     "description": "明示的に共有された未期限切れの粗い位置だけを読む。天気以外には使わない。",
     "inputSchema": {"type": "object", "properties": {
         "principal": {"type": "string", "description": "省略可。会話相手の legacy principal"}}}},
]


def call(service: BoardService, actor: Actor, name: str, args: dict, chat=None, media=None,
         accounts=None, mood=None, location=None, stickers=None) -> dict:
    if name == "location.get":
        if location is None:
            raise BoardError(503, "location_unavailable")
        return location.latest_for_agent(actor, principal=str(args.get("principal") or ""))
    if name == "sticker.list":
        if stickers is None:
            raise BoardError(503, "stickers_unavailable")
        entity = str(args.get("entity") or (actor.id if actor.kind == "entity" else "mira"))
        return stickers.list(actor, entity)
    if name.startswith("mood."):
        if mood is None:
            raise BoardError(503, "mood_unavailable")
        if name == "mood.list":
            return mood.list(actor)
        if name == "mood.set":
            if actor.kind != "entity":
                raise BoardError(403, "mood_entity_only")
            return mood.set(actor, args.get("key"), args.get("note") or "")
        raise BoardError(404, "unknown_tool")
    if name == "media.get":
        if media is None:
            raise BoardError(503, "media_unavailable")
        media_id = str(args.get("id") or args.get("media_id") or "")
        if not media_id:
            raise BoardError(400, "missing_id")
        return {"media": media.metadata(actor, media_id)}
    if name.startswith("chat."):
        if chat is None:
            raise BoardError(503, "chat_unavailable")
        conversation_id = str(args.get("conversation_id") or "")
        if name == "chat.conversation.open":
            return {"conversation": chat.open_direct(actor, str(args.get("account_id") or ""))}
        if name == "chat.agent_room.open":
            return {"conversation": chat.open_agent_room(actor)}
        if name == "chat.conversations":
            return chat.list(actor)
        if name == "chat.messages":
            return chat.messages(actor, conversation_id, before=str(args.get("before") or ""),
                                 after=str(args.get("after") or ""), limit=int(args.get("limit") or 50))
        if name == "chat.message.send":
            return chat.send(actor, conversation_id, text=args.get("text"), parts=args.get("parts"),
                             client_mutation_id=str(args.get("client_mutation_id") or ""),
                             causation_id=str(args.get("causation_id") or ""))
        raise BoardError(404, "unknown_tool")
    if name == "feed":
        raw_authors = args.get("authors") or ([args["author"]] if args.get("author") else [])
        authors = {str(item) for item in raw_authors if str(item)} or None
        if accounts is not None and authors is not None:
            resolved = {account.id for item in authors
                        if (account := accounts.resolve_legacy(item)) is not None}
            if len(resolved) != len(authors):
                raise BoardError(400, "unknown_author")
            authors = resolved
        return service.feed(actor, before=str(args.get("before") or ""),
                            limit=int(args.get("limit") or 20),
                            since=float(args.get("since") or 0.0), authors=authors)
    post_id = str(args.get("id") or "")
    if not post_id and name != "post":
        raise BoardError(400, "missing_id")
    if name == "get":
        return service.get(actor, post_id)
    if name == "post":
        return service.post(actor, args.get("text"), args.get("media") or [],
                            str(args.get("client_mutation_id") or ""),
                            source_ref=str(args.get("source_ref") or ""),
                            occurred_at=float(args.get("occurred_at") or 0.0))
    if name == "reply":
        return service.reply(actor, post_id, args.get("text"), args.get("media") or [],
                             str(args.get("client_mutation_id") or ""))
    if name == "like":
        return service.like(actor, post_id)
    if name == "delete":
        return service.delete(actor, post_id)
    raise BoardError(404, "unknown_tool")


def handle(message: dict, service: BoardService, actor: Actor, chat=None, media=None,
           accounts=None, mood=None, location=None, stickers=None) -> dict | None:
    method = str(message.get("method") or "")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "app-server", "version": "0.1.0"}}}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        tools = TOOLS + CHAT_TOOLS if chat is not None else TOOLS
        if media is not None:
            tools = tools + MEDIA_TOOLS
        if stickers is not None:
            tools = tools + STICKER_TOOLS
        if mood is not None:
            tools = tools + MOOD_TOOLS
        if location is not None:
            tools = tools + LOCATION_TOOLS
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}
    if method == "tools/call":
        try:
            payload = call(service, actor, str(params.get("name") or ""),
                           dict(params.get("arguments") or {}), chat=chat, media=media,
                           accounts=accounts, mood=mood, location=location, stickers=stickers)
            result = {"content": [{"type": "text",
                                   "text": json.dumps(payload, ensure_ascii=False)}]}
        except BoardError as exc:
            result = {"isError": True, "content": [{"type": "text", "text": exc.code}]}
        except (TypeError, ValueError) as exc:
            code = str(exc) if str(exc) == "bad_cursor" else "bad_request"
            result = {"isError": True, "content": [{"type": "text", "text": code}]}
        except Exception:
            result = {"isError": True, "content": [{"type": "text", "text": "internal_error"}]}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


__all__ = ["TOOLS", "call", "handle"]
