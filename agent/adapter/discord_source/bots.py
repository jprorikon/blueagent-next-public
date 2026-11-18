"""多顆 Discord bot：一顆 bot ↔ 一個實體（owner 2026-10-27「做 1 到 4」）。

背景：`blueagent-next` 原本只跑一顆 bot（mira），三個角色共用同一張臉、同一個在線狀態。
owner 交了 aster 自己的 bot token（`blue-agent-aster`）⇒ 現在每顆 bot 各自一份設定：

    # 主 bot（mira）：沿用無前綴那組
    DISCORD_BOT_TOKEN / GUILD_ID / TEXT_CHANNEL_ID / VOICE_CHANNEL_ID / ALLOWED_USER_IDS
    # 其他角色：`BLUEAGENT_DISCORD_<ENTITY>_*`（去前綴後長得跟主 bot 一樣）
    BLUEAGENT_DISCORD_ASTER_TOKEN / _GUILD_ID / _TEXT_CHANNEL_ID / _VOICE_CHANNEL_ID / _ENTITY
    # `_ALLOWED_USER_IDS` 不給就沿用主 bot 的 `ALLOWED_USER_IDS`（主人是同一個人）

`bots_from_env()` 只做純解析（可測），連線／語音在 `server/integrations/discord.py`。
"""

from __future__ import annotations

import os

#: 非主 bot 的前綴（entity 大寫）
PREFIX = "BLUEAGENT_DISCORD_"
#: 這些 suffix 不是「一顆 bot」的 token
_NOT_BOT_SUFFIX = ("ENTITY", "NAMES", "AUDIO", "REQUIRE_MENTION", "IGNORE_THREADS",
                   "FOLLOWUP_S", "TEXT_FIRST", "VC", "BOTS", "ENTITY_NAMES")


def bots_from_env(env) -> dict[str, dict]:
    """回 `{entity: env_map}`：每顆非主 bot 一份**去前綴**的環境（可直接餵 `DiscordConfig.from_env`）。

    認法：`BLUEAGENT_DISCORD_<ENTITY>_TOKEN`。entity 一律小寫（`ASTER` ⇒ `aster`）。
    """
    values = dict(env or os.environ)
    found: dict[str, dict] = {}
    for key in values:
        if not key.startswith(PREFIX) or not key.endswith("_TOKEN"):
            continue
        entity = key[len(PREFIX):-len("_TOKEN")]
        if not entity or entity in _NOT_BOT_SUFFIX:
            continue
        prefix = f"{PREFIX}{entity}_"
        mapped = {name[len(prefix):]: value for name, value in values.items()
                  if name.startswith(prefix)}
        # `..._ASTER_TOKEN` 去前綴後叫 `TOKEN`，但 `DiscordConfig.from_env` 認的是
        # `DISCORD_BOT_TOKEN`（與主 bot 同一把鍵）——這裡補一次別名。
        token = mapped.get("TOKEN") or mapped.get("DISCORD_BOT_TOKEN") or ""
        mapped["DISCORD_BOT_TOKEN"] = token
        # 主人是同一個人：沒給 `..._<ENTITY>_ALLOWED_USER_IDS` 就沿用主 bot 那份。
        # 否則白名單是空的 ⇒ 主人在 aster／nori 那顆被當成名單外，一句都不回（2026-10-27 審查）。
        if not str(mapped.get("ALLOWED_USER_IDS") or "").strip():
            mapped["ALLOWED_USER_IDS"] = str(values.get("ALLOWED_USER_IDS") or "")
        mapped["BLUEAGENT_DISCORD_ENTITY"] = entity.lower()
        mapped["DEFAULT_ENTITY"] = entity.lower()
        # 語音開關：`..._VC=1` 去前綴後是 `VC`，而 `VoiceConfig` 讀的是 `BLUEAGENT_DISCORD_VC`
        mapped["BLUEAGENT_DISCORD_VC"] = mapped.get("VC") or values.get("BLUEAGENT_DISCORD_VC") or "1"
        found[entity.lower()] = mapped
    return found


def sources_by_entity(sources) -> dict:
    """`[DiscordSource]` → `{entity: source}`（給主動訊息按角色挑 bot 用）。"""
    table = {}
    for source in sources or ():
        entity = str(getattr(getattr(source, "config", None), "default_entity", "") or "")
        if entity:
            table[entity] = source
    return table


__all__ = ["PREFIX", "bots_from_env", "sources_by_entity"]
