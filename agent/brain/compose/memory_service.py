"""記憶履歷與窗口摘要的唯讀應用服務。"""

from __future__ import annotations

from agent.brain.runtime.memory.fact_history import FactHistory, memory_db_path
from agent.brain.runtime.memory.window_store import WindowStore


class MemoryViewService:
    def __init__(self, app) -> None:
        self._app = app

    def trace(self, fact_key: str, *, limit: int = 20) -> list[dict]:
        return FactHistory(memory_db_path(self._app.memory)).for_fact(fact_key, limit=limit)

    def windows(self, entity: str, *, limit: int = 5) -> list[dict]:
        rows = WindowStore(memory_db_path(self._app.memory)).recent(entity, limit=limit)
        return [dict(row) for row in rows]
