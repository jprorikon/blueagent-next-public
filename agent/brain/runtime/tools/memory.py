"""`memory.search`：她在回合裡自己翻過去（三層，各守披露規則）。"""

from __future__ import annotations

from agent.brain.memory.facts import game_of
from agent.brain.runtime.tools.base import ToolContext, ToolResult


class MemorySearchTool:
    """`memory.search`：她自己在回合裡查過去（事實／窗口摘要／原文三層）。

    為什麼要有它：她以前只有「開頭被動塞進來的那幾條」，出了滾動窗就真的查不到。
    這個工具讓她能主動翻——但**披露規則照舊**：只回「在場的人都在房間裡」那些東西。
    """

    name = "memory.search"
    schema = {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": (
                "自分の記憶を調べる：前に何を話したか、あることの細部、ある期間の経過。"
                "主人が「前に言ってた…」「覚えてる？」「あのとき…」と言って自分が確かでないとき、まずこれで調べてから答える。"
                "三層で返る：facts（すでに記憶になった事実）／windows（各期間に何を話したかの要約）／"
                "turns（当時の原文）。見つからなければ覚えていないと正直に言う。作らない。"
                "facts は**いま成り立っているもの**だけが返る（新しい値に置き換えられた古い値は出ない）；"
                "「以前は何だったか」を聞かれたら days で遡るか、置き換え前の値も見える記憶の一覧を使う。"
                "（アスターから聞いた話）のように**前置きが付いた事実は、自分が直接聞いたのではなく誰かから聞いた話**："
                "そのまま「聞いた」と伝える。自分の体験として話さない。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "キーワード（例：「小説」「キャラシート」）"},
                    "limit": {"type": "integer", "description": "層ごとの最大件数（既定 5）"},
                    "days": {"type": "integer",
                             "description": "直近何日だけ見るか（省略＝制限なし）"},
                    "layers": {"type": "string",
                               "description": "fact,window,turn のカンマ区切り（省略＝三層すべて）。"
                                              "topic を足すと、自分の生活の**主題の一覧**（件数・最近の一言）も返る"},
                },
                "required": ["query"],
            },
        },
    }

    def __init__(self, search) -> None:
        self.search = search

    def run(self, ctx, arguments: dict) -> "ToolResult":
        import time
        args = dict(arguments or {})
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, error="empty_query")
        try:
            limit = max(1, min(20, int(args.get("limit") or 5)))
        except (TypeError, ValueError):
            limit = 5
        days = args.get("days")
        since = None
        try:
            if days not in (None, "", 0, "0"):
                since = time.time() - float(days) * 86400.0
        except (TypeError, ValueError):
            since = None
        raw_layers = str(args.get("layers") or "").strip()
        layers = tuple(part.strip() for part in raw_layers.split(",") if part.strip()) \
            if raw_layers else ("fact", "window", "turn")
        if self.search is None:
            return ToolResult(ok=False, error="memory_search_unavailable")
        # 披露看的是**整個房間**：群聊裡只帶說話的人，等於把主人的 1:1 當著客人查出來
        present = {str(who) for who in ctx.present} | {str(ctx.principal), str(ctx.entity)}
        # 場景也照回合走：現實回合只翻現實的、桌（TRPG）上只翻桌的（與被動召回同一條）
        result = self.search.search(entity=str(ctx.entity), present=present,
                                    query=query, limit=limit, since=since, layers=layers,
                                    game=game_of(ctx.source))
        if not result.get("ok"):
            return ToolResult(ok=False, error=str(result.get("error") or "search_failed"))
        return ToolResult(ok=True, data=result)


class MemoryOpenTool:
    """`memory.open`：拿門牌（`ptr`）回**逐字原文**那幾輪。

    為什麼要有它（2026-10-25）：檢索回的 window 摘要是轉述，轉述是二手污染的來源。
    有了門牌，她可以「翻回去看當時到底怎麼說的」——這是 ECHO 判據④「一個接口」的落點：
    `search` 吐門牌、`open` 吃門牌，兩支必須互相認得。
    """

    name = "memory.open"
    schema = {
        "type": "function",
        "function": {
            "name": "memory_open",
            "description": (
                "門牌を開いて当時の原文（逐語。要約ではない）。memory_search の各結果には ptr が付く。"
                "事実（facts）には代わりに key が付く：置き換えの鎖を辿るとき（この key を superseded_by／supersedes に持つ行）に使う。"
                "細部を確かめる・原文を引く・要約では足りないときに、その数輪を開く。"
                "一度に返る輪数には上限がある。more が出たらまだ開ききっていない。続きは more で開く。"
                "開けなければ見つからないと正直に言う。要約で作らない。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ptr": {"type": "string",
                            "description": "ポインタ。例：raw:mira#t=t_1295dc36d846（search 結果の ptr をそのまま写す）"},
                },
                "required": ["ptr"],
            },
        },
    }

    def __init__(self, search) -> None:
        self.search = search

    def run(self, ctx, arguments: dict) -> "ToolResult":
        args = dict(arguments or {})
        pointer = str(args.get("ptr") or args.get("pointer") or "").strip()
        if not pointer:
            return ToolResult(ok=False, error="empty_pointer")
        if self.search is None:
            return ToolResult(ok=False, error="memory_open_unavailable")
        # 披露看整個房間（與 memory.search 同一條）
        present = {str(who) for who in ctx.present} | {str(ctx.principal), str(ctx.entity)}
        result = self.search.open(entity=str(ctx.entity), present=present, pointer=pointer,
                                  game=game_of(ctx.source))
        if not result.get("ok"):
            return ToolResult(ok=False, error=str(result.get("error") or "open_failed"))
        return ToolResult(ok=True, data=result)


__all__ = ["MemoryOpenTool", "MemorySearchTool"]
