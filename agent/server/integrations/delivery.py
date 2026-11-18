"""主動發言的信道：Discord 上線後才註冊（DM 先、群後；私事不進群；語音在寂寞度高時優先）。

從 `server/main.main()` 拆出來（2026-10-25）。
"""

from __future__ import annotations


def _log(message: str) -> None:
    print(f"  {message}", flush=True)


def register_discord_channels(state, source, sources=None) -> None:
    try:
        from agent.brain.runtime.delivery.delivery import DiscordChannel, DiscordVoiceChannel
        router = getattr(state.app, "delivery", None)
        if router is None:
            return
        # `sources`：`{entity: source}`（多顆 bot）——主動訊息／語音按角色挑正確的 bot
        table = dict(sources or getattr(state.app, "discord_by_entity", {}) or {})
        channel = DiscordChannel(source, log=_log)
        channel.sources = table
        router.channels["discord"] = channel
        if "discord" not in router.order:
            router.order.append("discord")
        # 語音：寂寞度高時優先（她想要人聲，不是文字）
        if getattr(state.app, "voice", None) is not None:
            voice_channel = DiscordVoiceChannel(source, log=_log)
            voice_channel.sources = table
            router.channels["voice"] = voice_channel
            if "voice" not in router.order:
                router.order.append("voice")
    except Exception as exc:                          # noqa: BLE001
        print(f"  delivery: Discord 通道註冊失敗（{type(exc).__name__}: {exc}）", flush=True)


__all__ = ["register_discord_channels"]
