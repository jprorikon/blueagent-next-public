"""遊樂場：她自己的小筆記本（每個角色一份＋共用一份）。"""

from __future__ import annotations

from agent.brain.runtime.tools.base import ToolContext, ToolResult


class PlaygroundTool:
    """她自己的遊樂場（`own`）與大家共用的（`shared`）。

    `scope="own"` 由**呼叫者身份**決定路徑——模型指不到別人的資料夾。
    """

    name = "playground"
    #: 按需解鎖（技能 `playground`）：平常不放進給模型的清單（`tools/channels.py`）
    on_demand = True
    schema = {
        "type": "function",
        "function": {
            "name": "playground",
            "description": ("遊び場で読み書きする。action=list/read/write；"
                            "scope=own（自分だけが見える）または shared（みんなで共有）。"
                            "shared に書くと、あなたの名前と時刻が自動で付く。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "read", "write"]},
                    "scope": {"type": "string", "enum": ["own", "shared"]},
                    "name": {"type": "string", "description": "ファイル名（拡張子つき。例：note.md）"},
                    "content": {"type": "string", "description": "write のときに書く内容（プレーンテキスト）"},
                },
                "required": ["action"],
            },
        },
    }

    def __init__(self, playground) -> None:
        self.playground = playground

    def run(self, ctx, arguments: dict) -> "ToolResult":
        args = dict(arguments or {})
        action = str(args.get("action") or "list").strip().lower()
        scope = str(args.get("scope") or "own").strip().lower()
        if scope not in {"own", "shared"}:
            return ToolResult(ok=False, error="bad scope")
        if self.playground is None:
            return ToolResult(ok=False, error="playground_unavailable")
        name = str(args.get("name") or "").strip()
        try:
            if action == "list":
                result = self.playground.list(ctx.entity, scope=scope)
            elif action == "read":
                result = self.playground.read(ctx.entity, name, scope=scope)
            elif action == "write":
                result = self.playground.write(ctx.entity, name,
                                               str(args.get("content") or ""), scope=scope)
            else:
                return ToolResult(ok=False, error="bad action")
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(ok=bool(result.get("ok")), data=dict(result),
                          error=str(result.get("error") or ""))


__all__ = ["PlaygroundTool"]
