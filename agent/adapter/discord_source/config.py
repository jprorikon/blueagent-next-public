"""Discord 源的設定（`DISCORD_*`／`BLUEAGENT_DISCORD_*`）與她的叫名。"""

from __future__ import annotations

from dataclasses import dataclass, field


#: 被叫到名字就回（与 bootstrap 的唤醒词同一组）。
DEFAULT_NAMES = {"ミラ": "mira", "mira": "mira",
                 "アスター": "aster", "aster": "aster",
                 "ノリ": "nori", "ノリ": "nori", "nori": "nori"}


def _own_names(values) -> dict:
    """這顆 bot 自己的叫名（環境變數 `BLUEAGENT_DISCORD_NAMES`，逗號分隔）。

    預設＝`default_entity` 在 `DEFAULT_NAMES` 裡的那一組（mira ⇒ ミラ/mira）。
    **不含別的角色名**：喊「ノリ」不該叫醒 mira。
    """
    entity = _entity_of(values)
    raw = str(values.get("BLUEAGENT_DISCORD_NAMES") or "").strip()
    if raw:
        return {word.strip(): entity for word in raw.replace("，", ",").split(",")
                if word.strip()}
    return {word: mapped for word, mapped in DEFAULT_NAMES.items() if mapped == entity}


def _entity_of(values) -> str:
    """這顆 bot 代表哪個實體（owner 2026-10-27：aster／nori 各有自己的 bot）。

    多顆 bot 時，非主 bot 的那組環境變數由 `BLUEAGENT_DISCORD_<ENTITY>_*` 去前綴後餵進來，
    所以兩個鍵名都認：`BLUEAGENT_DISCORD_ENTITY`（原始）與 `DEFAULT_ENTITY`（去前綴後）。
    """
    raw = (values.get("BLUEAGENT_DISCORD_ENTITY") or values.get("DEFAULT_ENTITY") or "mira")
    return str(raw).strip().lower() or "mira"


@dataclass
class DiscordConfig:
    token: str = ""
    guild_id: str = ""
    channel_id: str = ""
    voice_channel_id: str = ""
    owner_ids: frozenset = frozenset()
    default_entity: str = "mira"
    reply_with_audio: bool = True
    require_mention: bool = False
    #: 忽略 thread 裡的消息（預設開）。理由：Hermes 自己也在同一串裡回話，
    #: 兩隻 bot 同時接同一句話會出現雙重回覆；要測就到大頻道說。
    ignore_threads: bool = True
    #: **這顆 bot 自己的名字**（只用來判斷「有沒有在叫她」），
    #: 不再做「名字 → 別的實體」的切換：一顆 bot 就是一個實體（owner 2026-10-22）。
    names: dict = field(default_factory=lambda: dict(DEFAULT_NAMES))
    #: 文字先發（owner 2026-10-24）：模型吐完就貼字，語音合成在後面追上。
    text_first: bool = True
    #: 她剛在這條頻道說完話的「延續窗」（秒）：窗內主人接著講、又沒 @ 別人 ⇒ 算在對她說。
    #: 0＝關掉這個判斷（只認 @ 與引用回覆）。owner 2026-10-22：「不 at 她但內容是回她」。
    followup_window_s: float = 180.0

    @classmethod
    def from_env(cls, values) -> "DiscordConfig":
        owners = {item.strip() for item in str(values.get("ALLOWED_USER_IDS") or "").split(",")
                  if item.strip()}
        return cls(
            token=str(values.get("DISCORD_BOT_TOKEN") or "").strip(),
            guild_id=str(values.get("GUILD_ID") or "").strip(),
            channel_id=str(values.get("TEXT_CHANNEL_ID") or "").strip(),
            voice_channel_id=str(values.get("VOICE_CHANNEL_ID") or "").strip(),
            owner_ids=frozenset(owners),
            default_entity=_entity_of(values),
            reply_with_audio=str(values.get("BLUEAGENT_DISCORD_AUDIO") or "1").strip()
            not in {"0", "off", "false", "no"},
            require_mention=str(values.get("BLUEAGENT_DISCORD_REQUIRE_MENTION") or "0").strip()
            in {"1", "on", "true", "yes"},
            ignore_threads=str(values.get("BLUEAGENT_DISCORD_IGNORE_THREADS") or "1").strip()
            not in {"0", "off", "false", "no"},
            names=_own_names(values),
            followup_window_s=float(values.get("BLUEAGENT_DISCORD_FOLLOWUP_S") or 180),
            text_first=str(values.get("BLUEAGENT_DISCORD_TEXT_FIRST") or "1").strip()
            not in {"0", "off", "false", "no"},
        )

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    @property
    def app_id(self) -> str:
        """bot token 的第一段是 base64 的 bot user id（＝ application id），入口連結要用它。"""
        head = str(self.token or "").split(".", 1)[0]
        if not head:
            return ""
        try:
            import base64
            padded = head + "=" * (-len(head) % 4)
            return base64.urlsafe_b64decode(padded).decode("ascii", "replace").strip()
        except Exception:                                # noqa: BLE001
            return ""
