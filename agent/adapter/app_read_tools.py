"""她從 App 取聊天前後文與帖子的工具（owner 2026-11-08「app 聊天的前後文更倾向于从 app 工具中去取，包括回复和 post」）。

App 已經是會話、消息、帖子的唯一真值；大腦自己那份對話記錄只是她看過的那幾則的副本。
所以「之前在聊什麼」「那條帖子寫了什麼」讓她直接問 App：

  · `app.chat.read`：讀她在場的會話（三人房／她和主人的私聊）。不給 `conversation_id` 就讀這一輪所在的會話；
    `before` 往前翻頁。App Server 用**她自己的帳號**判權限——客人和主人之間的私聊她讀不到。
  · `app.post.read`：給 `id` 讀一篇帖子全文＋一層回覆；不給就讀公共區最新幾篇。

只讀、只在 App 渠道出現（`channels=("app",)`），排在工具清單前面（模型對靠後的工具視而不見，見 wiring）。
寫入（發帖／回帖／點讚）仍走 MCP `board`，每日上限在那邊。
"""

from __future__ import annotations

import json
import os
import time

from agent.brain.runtime.tools.base import ToolResult

CHAT_LIMIT, CHAT_MAX = 15, 30
FEED_LIMIT = 8


def _conversation_of(source: str) -> str:
    raw = str(source or "")
    return raw[len("app:"):] if raw.startswith("app:") else ""


def _clock(at) -> str:
    try:
        return time.strftime("%m/%d %H:%M", time.localtime(float(at)))
    except (TypeError, ValueError, OverflowError):
        return ""


def message_line(message: dict, names: dict) -> str:
    author = message.get("author") or {}
    who, kind = str(author.get("id") or ""), str(author.get("kind") or "")
    name = "オーナー" if kind == "owner" else names.get(who, who)
    if message.get("deleted"):
        # 墓碑（App Server 2026-11-16）：作者收回了這則；不能讓她當成一張貼圖讀
        return f"{_clock(message.get('created_at'))} {name}：（取り消されたメッセージ）"
    parts = [part for part in (message.get("parts") or []) if isinstance(part, dict)]
    text = " ".join(str(part.get("text") or "").strip() for part in parts
                    if str(part.get("text") or "").strip())
    if not text:
        text = "（画像）" if any(part.get("kind") == "photo" for part in parts) else "（スタンプ）"
    return f"{_clock(message.get('created_at'))} {name}：{text[:200]}"


class _AppReadTool:
    channels = ("app",)

    def __init__(self, base_url: str, tokens: dict, names=None, client_factory=None) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.tokens = dict(tokens or {})
        self.names = dict(names or {})
        if client_factory is None:
            from agent.adapter.app_server import AppServerClient
            client_factory = AppServerClient
        self._client_factory = client_factory

    def _client(self, entity: str):
        token = self.tokens.get(str(entity))
        return self._client_factory(self.base_url, token) if token else None

    def _call(self, entity: str, name: str, arguments: dict):
        client = self._client(entity)
        if client is None:
            return None, ToolResult(ok=False, error="app_unavailable")
        try:
            return client.board_call(name, arguments), None
        except Exception as exc:                      # noqa: BLE001 - 讀不到就照實說，不編
            code = str(getattr(exc, "code", "") or type(exc).__name__)
            return None, ToolResult(ok=False, error=code,
                                    data={"text": "App から読めなかった。内容を推測で作らないこと。"})


class AppChatReadTool(_AppReadTool):
    name = "app.chat.read"
    schema = {"type": "function", "function": {
        "name": "app_chat_read",
        "description": ("App の会話（3人の部屋／オーナーとの個別チャット）の過去のやりとりを読む。"
                        "「さっき何の話だった？」「前に誰が何て言った？」のように、"
                        "手元に見えている分より前の流れが要るときに使う。"
                        "conversation_id を省くと今いる会話を読む。古い方へは before に前回の next_before を渡す。"),
        "parameters": {"type": "object", "properties": {
            "conversation_id": {"type": "string", "description": "省略可。省くと今いる会話"},
            "before": {"type": "string", "description": "省略可。前回結果の next_before（もっと前を読む）"},
            "limit": {"type": "integer", "description": f"省略可。何則読むか（既定 {CHAT_LIMIT}、最大 {CHAT_MAX}）"},
        }, "required": []}}}

    def run(self, ctx, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        conversation = str(args.get("conversation_id") or "").strip() \
            or _conversation_of(getattr(ctx, "source", ""))
        if not conversation:
            return ToolResult(ok=False, error="missing_conversation",
                              data={"text": "どの会話か分からない。conversation_id を指定する。"})
        limit = max(1, min(CHAT_MAX, int(args.get("limit") or CHAT_LIMIT)))
        query = {"conversation_id": conversation, "limit": limit}
        if str(args.get("before") or "").strip():
            query["before"] = str(args["before"]).strip()
        data, error = self._call(str(ctx.entity), "chat.messages", query)
        if error is not None:
            return error
        rows = [row for row in (data or {}).get("messages") or [] if isinstance(row, dict)]
        lines = [message_line(row, self.names) for row in rows]
        text = "\n".join(lines) if lines else "（この範囲にはやりとりがない）"
        more = str((data or {}).get("next_before") or "")
        if more:
            text += f"\n（もっと前がある：before={more}）"
        return ToolResult(ok=True, data={"text": text, "conversation_id": conversation,
                                         "count": len(lines), "next_before": more})


class AppPostReadTool(_AppReadTool):
    name = "app.post.read"
    schema = {"type": "function", "function": {
        "name": "app_post_read",
        "description": ("App の公共区の投稿を読む。id を渡すとその投稿の全文と返信、"
                        "省くと公共区の新しい投稿をいくつか。会話で投稿の話が出たとき、中身を確かめてから話す。"),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "省略可。投稿 id（pst_…）"},
        }, "required": []}}}

    def run(self, ctx, arguments: dict) -> ToolResult:
        post_id = str((arguments or {}).get("id") or "").strip()
        name, query = ("get", {"id": post_id}) if post_id else ("feed", {"limit": FEED_LIMIT})
        data, error = self._call(str(ctx.entity), name, query)
        if error is not None:
            return error
        rows = [(data or {}).get("post") or {}] if post_id else list((data or {}).get("posts") or [])
        replies = list((data or {}).get("replies") or []) if post_id else []
        lines = []
        for row in rows:
            who = "オーナー" if row.get("author_kind") == "owner" else \
                self.names.get(str(row.get("author_id") or ""), str(row.get("display") or ""))
            lines.append(f"[{row.get('id')}] {_clock(row.get('at'))} {who}：{str(row.get('body') or '')[:400]}"
                         f"（いいね {int(row.get('likes') or 0)}・返信 {int(row.get('replies') or 0)}）")
        for row in replies:
            who = "オーナー" if row.get("author_kind") == "owner" else \
                self.names.get(str(row.get("author_id") or ""), str(row.get("display") or ""))
            lines.append(f"　└ {_clock(row.get('at'))} {who}：{str(row.get('body') or '')[:200]}")
        return ToolResult(ok=True, data={"text": "\n".join(lines) or "（投稿がない）"})


#: 「上次看過之後」最多附幾則（再多就讓她自己用 app_chat_read 翻）
ROOM_NOTE_MAX = 8


def room_note(rows, *, conversation_id: str, since: float, skip_id: str, me: str, names: dict) -> str:
    """三人房這一輪附上的那一小段（hybrid：新的直接給，舊的用工具取）。

    只收「她上次真的看過之後」才出現、不是她自己、也不是觸發這一輪那則的發言；最多 `ROOM_NOTE_MAX` 則。
    """
    fresh = [row for row in rows or [] if isinstance(row, dict)
             and float(row.get("created_at") or 0.0) > float(since or 0.0)
             and str(row.get("id") or "") != str(skip_id)
             and str((row.get("author") or {}).get("id") or "") != str(me)][-ROOM_NOTE_MAX:]
    lines = [f"この部屋（conversation_id={conversation_id}）。"]
    if fresh:
        lines.append("前にスマホを見てから、今回の一言より前に出ていた発言（古い順。あなたはまだ読んでいなかった）：")
        lines.extend("・" + message_line(row, names) for row in fresh)
    lines.append("もっと前の流れが要るときは app_chat_read で読める。投稿の話なら app_post_read で中身を確かめる。")
    return "\n".join(lines)


def _tokens(raw: str) -> dict:
    raw = str(raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        return {str(k): str(v) for k, v in json.loads(raw).items() if str(k) and str(v)}
    pairs = (item.partition("=") for item in raw.split(","))
    return {key.strip(): value.strip() for key, sep, value in pairs if sep and key.strip() and value.strip()}


def build_app_read_tools(env=None, *, names=None) -> list:
    """沒接 App Server（沒有 URL 或角色 token）就一支都不給。"""
    env = os.environ if env is None else env
    base_url = str(env.get("BLUEAGENT_APP_SERVER_URL") or "").strip()
    tokens = _tokens(str(env.get("BLUEAGENT_APP_SERVER_TOKENS") or ""))
    if not base_url or not tokens:
        return []
    return [AppChatReadTool(base_url, tokens, names), AppPostReadTool(base_url, tokens, names)]


__all__ = ["AppChatReadTool", "AppPostReadTool", "build_app_read_tools", "message_line",
           "room_note"]
