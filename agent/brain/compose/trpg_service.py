"""TRPG hub 的應用服務；HTTP 層不直接認識 adapter。"""

from __future__ import annotations

from agent.adapter.trpg import hub as trpg_hub


class TrpgService:
    @staticmethod
    def token_available(entity: str) -> bool:
        return trpg_hub.token_available(entity)

    @staticmethod
    def tables(entity: str):
        return trpg_hub.list_tables(trpg_hub.hub_for(entity))

    @staticmethod
    def table(entity: str, table_id: str):
        return trpg_hub.table_summary(trpg_hub.hub_for(entity), table_id)

    @staticmethod
    def say(entity: str, table_id: str, text: str, *, seat: str = "", session: str = ""):
        return trpg_hub.say(trpg_hub.hub_for(entity), table_id, text, seat=seat, session=session)

    @staticmethod
    def enter(entity: str, table_id: str, *, char_id: str = ""):
        return trpg_hub.enter(trpg_hub.hub_for(entity), table_id, char_id=char_id)

    @staticmethod
    def act(entity: str, table_id: str, action: str, *, seat: str = "", session: str = "",
            target: str = "", attr: str = "", dc: int = 0, note: str = ""):
        return trpg_hub.act(trpg_hub.hub_for(entity), table_id, action, seat=seat,
                            session=session, target=target, attr=attr, dc=dc, note=note)
