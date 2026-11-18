"""語音動作：撥號／這一輪出聲／掛斷（她自己決定，不是 UI 開關）。"""

from __future__ import annotations

from agent.brain.runtime.tools.base import ToolContext, ToolResult


class VoiceCallTool:
    name = "voice.call"
    #: 主人在 App 或 Discord 跟她說話時才給（她決定打過去）；通話中已經在講了、TRPG 桌上不打電話（`tools/channels.py`）
    channels = ("app", "discord")
    schema = {
        "type": "function",
        "function": {
            "name": "voice.call",
            "description": "主人に自分から電話をかける。自分がかけたいと決めたときだけ呼ぶ。"
                           "end=phone は主人のスマホを鳴らす（出るかどうかは主人が決める）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "end": {"type": "string", "enum": ["discord", "phone"]},
                    "reason": {"type": "string"},
                    "if_unanswered": {"type": "string",
                                      "description": "出てもらえなかったとき送る一言。送らないなら空。"},
                },
                "required": [],
            },
        },
    }

    def run(self, ctx: ToolContext, arguments: dict) -> ToolResult:
        end = str(arguments.get("end") or "discord")
        if end == "phone":
            # 主人的 App 響鈴（接不接是主人的事）；結果由撥號器在背景寫進她的行動日誌
            result = ctx.voice.ring(ctx.entity, principal=ctx.principal,
                                    reason=str(arguments.get("reason") or ""),
                                    if_unanswered=str(arguments.get("if_unanswered") or ""),
                                    origin="chat", thread_id=ctx.thread_id)
        else:
            result = ctx.voice.call(ctx.entity, end=end,
                                    reason=str(arguments.get("reason") or ""),
                                    principal=ctx.principal, thread_id=ctx.thread_id)
        return ToolResult(ok=bool(result.get("ok")), data=dict(result),
                          frames=list(result.get("frames") or []),
                          error=str(result.get("error") or ""))


class VoiceSpeakTool:
    name = "voice.speak"
    #: App 與 Discord（Discord 源可以回音檔）；通話裡本來就是講的，TRPG 桌上沒有語音出口
    channels = ("app", "discord")
    schema = {
        "type": "function",
        "function": {
            "name": "voice.speak",
            "description": "音声で返事を話す。音声のほうが自然なときだけ呼ぶ。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "style": {"type": "string"},
                },
                "required": [],
            },
        },
    }

    def run(self, ctx: ToolContext, arguments: dict) -> ToolResult:
        return ToolResult(ok=True, data={
            "force_voice": True,
            "target": str(arguments.get("target") or "auto"),
            "style": str(arguments.get("style") or ""),
        })


class VoiceHangupTool:
    name = "voice.hangup"
    #: 能掛電話的地方：通話裡、Discord 語音頻道，也包括**通話進行中主人從 App／Discord 文字說「掛斷」**
    #: （`tests/test_demo_flows.py`：Discord 電話打著，主人在 App 說掛斷）；TRPG 桌上不給
    channels = ("app", "discord", "call", "discord_vc")
    schema = {
        "type": "function",
        "function": {
            "name": "voice.hangup",
            "description": "いまの音声通話を切る。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }

    def run(self, ctx: ToolContext, arguments: dict) -> ToolResult:
        result = ctx.voice.hangup(ctx.entity)
        return ToolResult(ok=bool(result.get("ok")), data=dict(result),
                          frames=list(result.get("frames") or []))


__all__ = ["VoiceCallTool", "VoiceSpeakTool", "VoiceHangupTool"]
