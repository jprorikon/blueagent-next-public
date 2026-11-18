"""訊息映射：誰在說話（Discord id → principal）、對誰說（mention／叫名 → entity）、要不要回。"""

from __future__ import annotations

import re

from agent.adapter.discord_source.config import DiscordConfig


_MENTION = re.compile(r"<@!?\d+>")


class DiscordRouter:
    """纯映射：身分、实体、該不該回。"""

    def __init__(self, config: DiscordConfig) -> None:
        self.config = config
        #: 由呼叫端在處理時設定（Discord 頻道對應）；純資料，方便測試。
        self.last_channel = ""

    def principal_for(self, discord_id: str) -> str:
        """**只認名單**：主人 → `owner`；其餘一律空字串（＝不認識這個人）。

        ⚠️ 這裡曾經回 `guest-<id>`——那等於「頻道裡任何人都自動變成 guest」，
        於是陌生人 @ 她一下就能拿到回覆（owner 2026-10-22 抓到的缺陷）。
        已批准的 guest 由 `DiscordSource._guest_principal()` 另外查（需要 state）。
        """
        discord_id = str(discord_id or "").strip()
        if discord_id and discord_id in self.config.owner_ids:
            return "owner"
        return ""

    def is_owner(self, discord_id: str) -> bool:
        return self.principal_for(discord_id) == "owner"

    def entity_for(self, text: str) -> str:
        """**永遠是這顆 bot 的實體**（一顆 bot ↔ 一個實體，owner 2026-10-22）。

        以前這裡會按名字切換（喊「ノリ」就讓 nori 回）——那等於一顆 mira 的帳號
        冒出別人的聲音與頭像，也讓「誰在回」變得不確定。aster/nori 要有自己的 bot 時，
        各自建應用、各自一份 `default_entity`，而不是共用這一顆。
        """
        return self.config.default_entity

    def addressed_hint(self, text: str, *, mentioned: bool, is_dm: bool) -> str:
        """**只是給注意力的輸入**，不是「要不要回」的答案。

        多人頻道裡「回不回」由 Router／AttentionGate 判（observe／react／yield／reply 的評分、
        發言權租約、插話抑制）。這裡只提供「有沒有被直接叫到」這個事實：
        DM、被 @、或文字裡出現她的名字 ⇒ 回主權的候選。
        """
        if is_dm or mentioned:
            return "direct"
        body = str(text or "")
        if any(word and word in body for word in self.config.names):
            return "named"
        return "ambient"

    def clean_text(self, text: str) -> str:
        return _MENTION.sub("", str(text or "")).strip()
