"""平台公开展示心情：独立社交状态，不读取或复制 Agent 私有 affect。"""

from __future__ import annotations

import sqlite3
import threading
import time

from app.server.events import append_event
from app.server.social.models import Actor, BoardError

MOOD_KEYS = frozenset({"calm", "happy", "excited", "irritated", "down", "lonely"})
MAX_NOTE = 80

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mood_state(
  account_id TEXT PRIMARY KEY,
  mood_key TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL,
  event_id TEXT NOT NULL
);
"""


class MoodService:
    def __init__(self, db_path, accounts, *, now=time.time) -> None:
        self.db_path = str(db_path)
        self.accounts = accounts
        self.now = now
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _can_see(viewer, subject) -> bool:
        return subject.kind != "entity" or viewer.visible_entities is None \
            or subject.id in viewer.visible_entities or viewer.id == subject.id

    @staticmethod
    def _pack(row, account) -> dict:
        return {"account_id": account.id, "kind": account.kind, "display": account.display,
                "key": row["mood_key"], "note": row["note"],
                "updated_at": float(row["updated_at"]), "event_id": row["event_id"]}

    @staticmethod
    def _validate(key, note) -> tuple[str, str]:
        key = str(key or "").strip().lower()
        if key not in MOOD_KEYS:
            raise BoardError(400, "invalid_mood", allowed=sorted(MOOD_KEYS))
        # 公开短句是单行状态；私有 affect 数值和任意额外字段一律不进入此模型。
        note = " ".join(str(note or "").split())
        if len(note) > MAX_NOTE:
            raise BoardError(413, "mood_note_too_long", limit=MAX_NOTE)
        return key, note

    def get(self, actor: Actor) -> dict:
        account = self.accounts.get(actor.id)
        if account is None:
            raise BoardError(403, "unknown_account")
        with self._lock:
            row = self._db.execute("SELECT * FROM mood_state WHERE account_id=?",
                                   (account.id,)).fetchone()
        return {"mood": self._pack(row, account) if row is not None else None}

    def list(self, actor: Actor) -> dict:
        viewer = self.accounts.get(actor.id)
        if viewer is None:
            raise BoardError(403, "unknown_account")
        result = []
        with self._lock:
            for account in self.accounts.all():
                if not self._can_see(viewer, account):
                    continue
                row = self._db.execute("SELECT * FROM mood_state WHERE account_id=?",
                                       (account.id,)).fetchone()
                if row is not None:
                    result.append(self._pack(row, account))
        return {"moods": result}

    def set(self, actor: Actor, key, note="") -> dict:
        account = self.accounts.get(actor.id)
        if account is None:
            raise BoardError(403, "unknown_account")
        key, note = self._validate(key, note)
        with self._lock, self._db:
            current = self._db.execute("SELECT * FROM mood_state WHERE account_id=?",
                                       (account.id,)).fetchone()
            if current is not None and current["mood_key"] == key and current["note"] == note:
                return {"mood": self._pack(current, account), "idempotent_replay": True}
            at = float(self.now())
            mood = {"account_id": account.id, "kind": account.kind, "display": account.display,
                    "key": key, "note": note, "updated_at": at}
            audience = [viewer.id for viewer in self.accounts.all()
                        if self._can_see(viewer, account)]
            event_id = append_event(
                self._db, event_type="mood.updated", at=at,
                actor_id=account.id, actor_kind=account.kind,
                entity_kind="account", entity_id=account.id,
                payload={"mood": mood}, audience=audience)
            self._db.execute("""
              INSERT INTO mood_state(account_id,mood_key,note,updated_at,event_id)
              VALUES (?,?,?,?,?)
              ON CONFLICT(account_id) DO UPDATE SET mood_key=excluded.mood_key,
                note=excluded.note,updated_at=excluded.updated_at,event_id=excluded.event_id
            """, (account.id, key, note, at, event_id))
            row = self._db.execute("SELECT * FROM mood_state WHERE account_id=?",
                                   (account.id,)).fetchone()
        return {"mood": self._pack(row, account), "event_id": event_id,
                "idempotent_replay": False}


__all__ = ["MAX_NOTE", "MOOD_KEYS", "MoodService"]
