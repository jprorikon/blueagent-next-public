"""App Server 持久事件日志：业务库同事务追加，按序号游标同步与重连。"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading

from app.server.kicker import EventsKicker, targets_for

#: 同一個請求執行緒裡「剛寫進去、還沒叫醒」的事件（見 `append_event`）。
#: 用 thread-local 是因為寫入方是各個 Store（只有 db，沒有 EventLog 參考），
#: 而 flush 統一由 HTTP 層在回應前做一次——**順序保證：先提交、再叫醒**。
_PENDING = threading.local()


def _pending() -> list[dict]:
    items = getattr(_PENDING, "items", None)
    if items is None:
        items = []
        _PENDING.items = items
    return items


EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL,
  occurred_at REAL NOT NULL,
  actor_id TEXT NOT NULL,
  actor_kind TEXT NOT NULL,
  entity_kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  causation_id TEXT
);
CREATE INDEX IF NOT EXISTS event_type ON event(type, seq);
CREATE TABLE IF NOT EXISTS event_audience (
  event_seq INTEGER NOT NULL, account_id TEXT NOT NULL,
  PRIMARY KEY (event_seq, account_id)
);
CREATE INDEX IF NOT EXISTS event_audience_account ON event_audience(account_id, event_seq);
"""


def append_event(db: sqlite3.Connection, *, event_type: str, at: float,
                 actor_id: str, actor_kind: str, entity_kind: str, entity_id: str,
                 payload: dict, causation_id: str = "", audience=None) -> str:
    """调用方必须已处于业务事务中；这里不另起事务，保证一起提交／回滚。

    `audience=None` 是公开事件；给定账号列表时，只有这些账号能读到。
    """
    event_id = "evt_" + secrets.token_hex(12)
    cursor = db.execute("""
      INSERT INTO event(event_id,type,occurred_at,actor_id,actor_kind,
                        entity_kind,entity_id,payload,causation_id)
      VALUES (?,?,?,?,?,?,?,?,?)
    """, (event_id, str(event_type), float(at), str(actor_id), str(actor_kind),
          str(entity_kind), str(entity_id),
          json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
          str(causation_id or "") or None))
    if audience is not None:
        for account_id in sorted({str(item) for item in audience if str(item)}):
            db.execute("INSERT INTO event_audience(event_seq,account_id) VALUES (?,?)",
                       (int(cursor.lastrowid), account_id))
    # 只登記，不發送：叫醒必須等這個事務提交（由 HTTP 層的 `flush()` 做）。
    _pending().append({"event_type": str(event_type), "payload": dict(payload or {}),
                       "actor_id": str(actor_id), "actor_kind": str(actor_kind)})
    return event_id


def _envelope(row: sqlite3.Row) -> dict:
    return {
        "schema_version": 1,
        "event_id": row["event_id"],
        "cursor": str(row["seq"]),
        "type": row["type"],
        "occurred_at": float(row["occurred_at"]),
        "actor": {"kind": row["actor_kind"], "id": row["actor_id"]},
        "entity": {"kind": row["entity_kind"], "id": row["entity_id"]},
        "payload": json.loads(row["payload"]),
        "causation_id": row["causation_id"] or "",
    }


class EventLog:
    """独立读取连接可跨线程使用；写入仍由拥有业务事务的 Store 完成。"""

    def __init__(self, db_path, *, kicker: EventsKicker | None = None, accounts=None,
                 log=None) -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        self._kicker = kicker
        self._accounts = accounts
        self._log = log or (lambda _message: None)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── 叫醒（規格 `docs/PLAN-app-kick-20261108.md`）────────────────────────────
    def flush(self) -> int:
        """把這一輪請求寫入的事件翻成「該叫醒誰」並丟給 kicker。

        **必須在業務事務提交之後呼叫**（現役唯一呼叫點：`AppHandler.do_POST` 的 finally），
        否則 Agent 被叫醒時還看不到那條事件。回傳實際叫醒的事件條數（給測試與日誌用）。
        """
        queue = _pending()
        if not queue:
            return 0
        items, queue[:] = list(queue), []
        kicker = self._kicker
        if kicker is None or not kicker.enabled():
            return 0
        woken = 0
        for item in items:
            try:
                targets = targets_for(event_type=item["event_type"], payload=item["payload"],
                                      actor_id=item["actor_id"], actor_kind=item["actor_kind"],
                                      lookup_post=self._post_author, is_entity=self._is_entity)
            except Exception as exc:       # noqa: BLE001 - 判據壞了不能擋發消息
                self._log(f"kick: 判據失敗 {item['event_type']}（{type(exc).__name__}: {exc}）")
                continue
            if not targets:
                continue
            kicker.kick(targets, reason=item["event_type"])
            woken += 1
        return woken

    def _post_author(self, post_id: str):
        with self._lock:
            row = self._db.execute(
                "SELECT author_id, author_kind FROM post WHERE id=?", (str(post_id),)).fetchone()
        return (row["author_id"], row["author_kind"]) if row is not None else None

    def _is_entity(self, account_id: str) -> bool:
        if self._accounts is None:
            return False
        return str(getattr(self._accounts.get(str(account_id)), "kind", "")) == "entity"

    @staticmethod
    def _after(value) -> int:
        try:
            cursor = int(str(value or "0"))
        except (TypeError, ValueError) as exc:
            raise ValueError("bad_cursor") from exc
        if cursor < 0:
            raise ValueError("bad_cursor")
        return cursor

    def normalize_cursor(self, value) -> str:
        """在进入 SSE 响应前验证并规范化游标。"""
        return str(self._after(value))

    def list(self, *, after=0, limit: int = 100, account_id: str | None = None) -> dict:
        cursor = self._after(after)
        limit = max(1, min(200, int(limit or 100)))
        with self._lock:
            if account_id is None:
                rows = self._db.execute(
                    "SELECT * FROM event WHERE seq>? ORDER BY seq LIMIT ?",
                    (cursor, limit + 1)).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT e.* FROM event AS e WHERE e.seq>? AND "
                    "(NOT EXISTS(SELECT 1 FROM event_audience a WHERE a.event_seq=e.seq) "
                    "OR EXISTS(SELECT 1 FROM event_audience a WHERE a.event_seq=e.seq AND a.account_id=?)) "
                    "ORDER BY e.seq LIMIT ?",
                    (cursor, str(account_id), limit + 1)).fetchall()
        selected = rows[:limit]
        events = [_envelope(row) for row in selected]
        return {"events": events,
                "next_cursor": str(selected[-1]["seq"]) if selected else str(cursor),
                "has_more": len(rows) > limit}

    def cursor_for_event(self, event_id: str) -> str:
        with self._lock:
            row = self._db.execute("SELECT seq FROM event WHERE event_id=?",
                                   (str(event_id),)).fetchone()
        if row is None:
            raise ValueError("cursor_expired")
        return str(row["seq"])


__all__ = ["EVENT_SCHEMA", "EventLog", "append_event"]
