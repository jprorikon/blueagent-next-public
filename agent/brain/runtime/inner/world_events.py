"""一次性世界事件：共享环境与限定范围的房间事件分开投递。

事件只留在短期内存队列；观察者把已见 id 落在自己的 world 状态里。
公开输出只收显式 ``public=True`` 的事件，桌／语音房事件永不过公开路径。
"""

from __future__ import annotations

import collections
import threading
import time
import uuid


class WorldEvents:
    def __init__(self, *, keep: int = 512, clock=time.time) -> None:
        self._rows = collections.deque(maxlen=max(32, int(keep)))
        self._clock = clock
        self._lock = threading.RLock()

    def publish(self, kind: str, text: str, *, entities=(), principals=(),
                guild_id: str = "", table_id: str = "", public: bool = False) -> dict:
        body = str(text or "").strip()[:160]
        if not body:
            return {}
        row = {
            "id": uuid.uuid4().hex,
            "at": round(float(self._clock()), 3),
            "kind": str(kind or "world"),
            "text": body,
            "entities": frozenset(str(item) for item in (entities or ()) if str(item)),
            "principals": frozenset(str(item) for item in (principals or ()) if str(item)),
            "guild_id": str(guild_id or ""),
            "table_id": str(table_id or ""),
            "public": bool(public),
        }
        with self._lock:
            self._rows.append(row)
        return row

    def pending(self, entity: str, seen, *, principal: str = "owner",
                guild_ids=(), table_ids=(), public: bool = False) -> list[dict]:
        seen_ids = {str(item) for item in (seen or ())}
        guilds = {str(item) for item in (guild_ids or ()) if str(item)}
        tables = {str(item) for item in (table_ids or ()) if str(item)}
        with self._lock:
            rows = list(self._rows)
        found = []
        for row in rows:
            if row["id"] in seen_ids:
                continue
            if public and not row["public"]:
                continue
            if row["entities"] and str(entity) not in row["entities"]:
                continue
            if row["principals"] and str(principal) not in row["principals"]:
                continue
            if row["guild_id"] and row["guild_id"] not in guilds:
                continue
            if row["table_id"] and row["table_id"] not in tables:
                continue
            found.append({"id": row["id"], "kind": row["kind"], "text": row["text"]})
        return found


__all__ = ["WorldEvents"]
