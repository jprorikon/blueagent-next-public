"""Discord 源：bot 在频道里听、把话送进同一条管道、把回复（和语音）发回去。

移管背景：2026-10-22 owner 把现役 `blueagent-discord-nubia-nx733j` 用的那颗 bot 交给新版本，
旧栈（`blueagent-server` / `blueagent-discord-*`）同时下线 ⇒ **同一 token 不会两处连**（会互踢）。

分层（为什么这样切）：
  · `DiscordRouter`：纯逻辑——谁说的话算谁的、什么情况该回、实体怎么选。**不碰网络**，可单测。
  · `DiscordSource`：只做 discord.py 的适配（Gateway 事件 ↔ 上面那层），以及把回话发回频道。
  · 真正的回合执行仍然走 `AppState.stream()`——与 HTTP 打字进来的是同一条管道，
    所以权限（guest 只能 chat）、记忆（ACL）、委任（owner＋user.md）全部照旧，不因来源不同而放宽。

身分映射：`ALLOWED_USER_IDS` 里的人 = 主人（principal `owner`），其他人 = `guest-<discord_id>`。
**不看昵称、不看头像**——名字可以改，id 不会。

按職責拆開（2026-10-25 review P3）：設定（config）、訊息映射（routing：Discord id／mention → principal／entity）、
傳輸與連線生命週期（transport）、語音與音檔（speech）、源本體（source：一則訊息怎麼變成一輪）。
`from adapter.discord_source import X` 照用。
"""

from __future__ import annotations

from agent.adapter.discord_source.config import DEFAULT_NAMES, _own_names, DiscordConfig  # noqa: F401
from agent.adapter.discord_source.routing import _MENTION, DiscordRouter  # noqa: F401
from agent.adapter.discord_source.source import DiscordSource, build_discord  # noqa: F401

__all__ = ["DEFAULT_NAMES", "DiscordConfig", "DiscordRouter", "DiscordSource", "build_discord"]
