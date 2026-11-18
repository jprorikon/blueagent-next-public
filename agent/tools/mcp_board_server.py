#!/usr/bin/env python3
"""公共区与公开心情 stdio MCP；平台模式下全部经 App Server 远程写入。"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.adapter.app_board import RemoteBoardService  # noqa: E402
from agent.adapter.app_protocol import Actor, BoardError  # noqa: E402
from agent.adapter.app_server import _tokens  # noqa: E402

BOARD_ACTOR = str(os.environ.get("BOARD_ACTOR") or "").strip()
#: owner 2026-11-07：「工具按照你说的做」——回帖／點讚要帶閘，不然她一天能把公共區刷成流水帳。
#: 用量按角色按天記在 `BOARD_DATA/board-access.json`（跨行程一致：MCP server 每輪可能是新行程）。
REPLY_PER_DAY = int(os.environ.get("BOARD_REPLY_PER_DAY") or 5)
LIKE_PER_DAY = int(os.environ.get("BOARD_LIKE_PER_DAY") or 10)
#: 發帖（owner 2026-11-08「发帖可以给她」）：一天最多幾條。發帖本身還會按一次外網計價。
POST_PER_DAY = int(os.environ.get("BOARD_POST_PER_DAY") or 3)


def _usage_path() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("BOARD_DATA") or ".")
    return root / "board-access.json"


@contextlib.contextmanager
def _usage(now=None):
    """讀改寫用量檔都在檔案鎖裡（REVIEW 2026-11-08 A7）：MCP server 可能每輪一個新行程，
    幾個角色同時發帖時，沒有鎖就會互相蓋掉計數、讓上限失效。"""
    path = _usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            today = time.strftime("%Y%m%d", time.localtime(now if now is not None else time.time()))
            if payload.get("day") != today:
                payload = {"day": today, "counts": {}, "replied": []}
            yield payload
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True),
                                 encoding="utf-8")
            os.replace(temporary, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _spend(actor_id: str, kind: str, limit: int, *, post_id: str = "", now=None) -> str:
    """先預留一次用量；回空字串＝放行，否則回機器可讀的拒絕理由。呼叫失敗要 `_refund`。"""
    if int(limit) <= 0:                      # 0／負數＝這條不設限（測試／除錯）
        return ""
    with _usage(now) as payload:
        counts = payload.setdefault("counts", {}).setdefault(str(actor_id), {})
        key = f"{actor_id}:{post_id}" if post_id else ""
        if kind == "reply" and key and key in set(payload.get("replied") or []):
            return "already_replied"
        if int(counts.get(kind, 0)) >= int(limit):
            return f"{kind}_daily_limit"
        counts[kind] = int(counts.get(kind, 0)) + 1
        if kind == "reply" and key:
            replied = list(payload.get("replied") or [])[-200:]
            replied.append(key)
            payload["replied"] = replied
    return ""


def _refund(actor_id: str, kind: str, *, post_id: str = "", now=None) -> None:
    """App Server 那邊沒成功 ⇒ 把預留的用量退回（REVIEW 2026-11-08 A5：以前失敗也算一次，
    回帖失敗後那條帖子更是永遠 `already_replied`，改好再回也回不了）。"""
    with _usage(now) as payload:
        counts = payload.setdefault("counts", {}).setdefault(str(actor_id), {})
        if int(counts.get(kind, 0)) > 0:
            counts[kind] = int(counts[kind]) - 1
        key = f"{actor_id}:{post_id}" if post_id else ""
        if kind == "reply" and key:
            payload["replied"] = [row for row in (payload.get("replied") or []) if row != key]


_PROPS = {
    "id": {"type": "string", "description": "投稿 ID"},
    "text": {"type": "string", "description": "公开正文（最多 2000 字）"},
    "media": {"type": "array", "items": {"type": "string"},
              "description": "既有 media_id，最多 4 件"},
    "client_mutation_id": {"type": "string", "description": "重试沿用同一幂等键"},
    "source_ref": {"type": "string", "description": "公开源的稳定引用；重复发布返回原帖"},
}
TOOLS = [
    {"name": "feed", "description": "公共区を新しい順で読む。before と limit でページング。",
     "inputSchema": {"type": "object", "properties": {
         "before": {"type": "string"}, "limit": {"type": "integer"},
         "author": {"type": "string", "description": "作者 id（省略＝全部）"}}}},
    {"name": "get", "description": "一つの投稿全文と一階層の公開返信を読む。",
     "inputSchema": {"type": "object", "properties": {"id": _PROPS["id"]}, "required": ["id"]}},
    {"name": "post", "description": "公開したい内容だけを投稿する。source_ref で公開元を冪等に紐付けられる。",
     "inputSchema": {"type": "object", "properties": {
         "text": _PROPS["text"], "media": _PROPS["media"],
         "client_mutation_id": _PROPS["client_mutation_id"],
         "source_ref": _PROPS["source_ref"]}, "required": ["text"]}},
    {"name": "reply", "description": "他人の公開投稿へ一階層だけ返信する。自分への返信や私聊には使わない。",
     "inputSchema": {"type": "object", "properties": {
         "id": _PROPS["id"], "text": _PROPS["text"], "media": _PROPS["media"],
         "client_mutation_id": _PROPS["client_mutation_id"]},
         "required": ["id", "text"]}},
    {"name": "like", "description": "他人の投稿へいいね（同じ投稿へ二度押しても一件のまま）。",
     "inputSchema": {"type": "object", "properties": {"id": _PROPS["id"]}, "required": ["id"]}},
    {"name": "delete", "description": "自分の投稿を論理削除。他人の削除は主人だけ。記録整理には使わない。",
     "inputSchema": {"type": "object", "properties": {"id": _PROPS["id"]}, "required": ["id"]}},
    {"name": "mood.list", "description": "自分に見えるアカウントの公開気分を読む。私有 affect ではない。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mood.set", "description": "自分の公開気分を明示的に設定する。心の内側を自動公開する道具ではない。",
     "inputSchema": {"type": "object", "properties": {
         "key": {"type": "string", "enum": ["calm", "happy", "excited", "irritated", "down", "lonely"]},
         "note": {"type": "string", "description": "公開する一言（任意、80 字まで）"}},
         "required": ["key"]}},
]


def _service() -> RemoteBoardService:
    """S5：stdio 也只是远程客户端，不能打开 Agent data/ 下的平台数据库。"""
    tokens = _tokens(os.environ.get("BLUEAGENT_APP_SERVER_TOKENS") or "")
    base_url = str(os.environ.get("BLUEAGENT_APP_SERVER_URL") or "").strip()
    return RemoteBoardService(base_url, lambda actor: tokens.get(actor.id, ""))


def _actor(arguments: dict | None = None) -> Actor:
    # from:entity 只接受应用 inject 的隐藏参数；独立 stdio 则固定 BOARD_ACTOR。
    identity = str((arguments or {}).get("entity") or "") if BOARD_ACTOR == "from:entity" \
        else BOARD_ACTOR
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", identity):
        raise BoardError(403, "invalid_board_actor")
    kind = "owner" if identity == "owner" else "entity"
    return Actor(identity, kind, identity)


def _ok(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _fail(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": str(message)}]}


def _call(service: RemoteBoardService, name: str, args: dict) -> dict:
    actor = _actor(args)
    limits = {"post": POST_PER_DAY, "reply": REPLY_PER_DAY, "like": LIKE_PER_DAY}
    post_id = str(args.get("id") or "") if name == "reply" else ""
    if name in limits:
        refused = _spend(actor.id, name, limits[name], post_id=post_id)
        if refused:
            raise BoardError(429, refused)
    try:
        return _dispatch(service, name, args, actor)
    except Exception:
        if name in limits and limits[name] > 0:
            _refund(actor.id, name, post_id=post_id)
        raise


def _dispatch(service: RemoteBoardService, name: str, args: dict, actor: Actor) -> dict:
    if name == "mood.list":
        if not hasattr(service, "list_moods"):
            raise BoardError(503, "mood_unavailable")
        return service.list_moods(actor)
    if name == "mood.set":
        if actor.kind != "entity":
            raise BoardError(403, "mood_entity_only")
        if not hasattr(service, "set_mood"):
            raise BoardError(503, "mood_unavailable")
        return service.set_mood(actor, args.get("key"), args.get("note") or "")
    if name == "feed":
        authors = {str(args["author"])} if args.get("author") else None
        return service.feed(actor, before=str(args.get("before") or ""),
                            limit=int(args.get("limit") or 20), authors=authors)
    post_id = str(args.get("id") or "")
    if not post_id and name != "post":
        raise BoardError(400, "missing_id")
    if name == "get":
        return service.get(actor, post_id)
    if name == "post":
        return service.post(actor, args.get("text"), args.get("media") or [],
                            str(args.get("client_mutation_id") or ""),
                            source_ref=str(args.get("source_ref") or ""))
    if name == "reply":
        return service.reply(actor, post_id, args.get("text"), args.get("media") or [],
                             str(args.get("client_mutation_id") or ""))
    if name == "like":
        return service.like(actor, post_id)
    if name == "delete":
        return service.delete(actor, post_id)
    raise BoardError(404, "unknown_tool")


def handle(message: dict, service: RemoteBoardService | None = None) -> dict | None:
    method = str(message.get("method") or "")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "board", "version": "1.0"}}}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        try:
            local = service or _service()
            payload = _ok(_call(local, str(params.get("name") or ""),
                                dict(params.get("arguments") or {})))
        except BoardError as exc:
            payload = _fail(exc.code)
        except Exception as exc:  # noqa: BLE001 - 工具错误不能杀死 stdio server
            payload = _fail(f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": payload}
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> int:
    service = _service()
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = handle(message, service)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
