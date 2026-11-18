"""TRPG 席位的設定（`BLUEAGENT_TRPG_*`）。"""

from __future__ import annotations



class TrpgConfig:
    """一桌一設定（`BLUEAGENT_TRPG_*`）。"""

    def __init__(self, *, enabled: bool = False, hub_url: str = "http://127.0.0.1:18971",
                 token_file: str = "", table_id: str = "", session: str = "",
                 seat: str = "", entity: str = "mira", kick_token: str = "",
                 gm_token: str = "", speak_lang: str = "ja") -> None:
        self.enabled = bool(enabled)
        self.hub_url = str(hub_url).rstrip("/")
        self.token_file = str(token_file or "")
        self.table_id = str(table_id or "")
        self.session = str(session or "")
        self.seat = str(seat or "")
        self.entity = str(entity or "mira")
        self.kick_token = str(kick_token or "")
        self.gm_token = str(gm_token or "")
        self.speak_lang = str(speak_lang or "ja")

    @classmethod
    def from_env(cls, values) -> "TrpgConfig":
        def flag(name: str) -> bool:
            return str(values.get(name) or "").strip().lower() in {"1", "on", "true", "yes"}

        return cls(
            enabled=flag("BLUEAGENT_TRPG"),
            hub_url=str(values.get("BLUEAGENT_TRPG_HUB") or "http://127.0.0.1:18971"),
            token_file=str(values.get("BLUEAGENT_TRPG_TOKEN_FILE") or ""),
            table_id=str(values.get("BLUEAGENT_TRPG_TABLE") or ""),
            session=str(values.get("BLUEAGENT_TRPG_SESSION") or ""),
            seat=str(values.get("BLUEAGENT_TRPG_SEAT") or ""),
            entity=str(values.get("BLUEAGENT_TRPG_ENTITY") or "mira"),
            kick_token=str(values.get("BLUEAGENT_TRPG_KICK_TOKEN") or ""),
            gm_token=str(values.get("BLUEAGENT_TRPG_GM_TOKEN") or ""),
            speak_lang=str(values.get("BLUEAGENT_TRPG_LANG") or "ja"),
        )

    @property
    def ready(self) -> bool:
        # 桌與 session 由 kick 帶進來（見 `TrpgSeat.adopt`）；公開 kick 入口則必須有獨立令牌。
        return bool(self.enabled and self.seat and self.kick_token)
