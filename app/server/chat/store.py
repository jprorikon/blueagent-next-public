"""会话、消息、分段与已读游标的 SQLite 持久化。"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading

from app.server.events import append_event
from app.server.social.models import BoardError

CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at REAL NOT NULL,
  created_by TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conversation_participant (
  conversation_id TEXT NOT NULL, account_id TEXT NOT NULL, joined_at REAL NOT NULL,
  PRIMARY KEY (conversation_id, account_id)
);
CREATE INDEX IF NOT EXISTS participant_account
  ON conversation_participant(account_id, conversation_id);
CREATE TABLE IF NOT EXISTS message (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
  conversation_id TEXT NOT NULL, author_id TEXT NOT NULL, author_kind TEXT NOT NULL,
  display TEXT NOT NULL, created_at REAL NOT NULL,
  deleted_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS message_conversation ON message(conversation_id, seq);
CREATE TABLE IF NOT EXISTS message_part (
  message_id TEXT NOT NULL, part_index INTEGER NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '', media_id TEXT NOT NULL DEFAULT '',
  sticker TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (message_id, part_index)
);
CREATE TABLE IF NOT EXISTS read_cursor (
  conversation_id TEXT NOT NULL, account_id TEXT NOT NULL, message_seq INTEGER NOT NULL,
  updated_at REAL NOT NULL, PRIMARY KEY (conversation_id, account_id)
);
CREATE TABLE IF NOT EXISTS legacy_turn_message (
  principal TEXT NOT NULL, entity TEXT NOT NULL, turn_id TEXT NOT NULL,
  direction TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE, imported_at REAL NOT NULL,
  PRIMARY KEY (principal, entity, turn_id, direction)
);
"""


def direct_id(first: str, second: str) -> str:
    pair = "\0".join(sorted((str(first), str(second))))
    return "cnv_" + hashlib.sha256(pair.encode("utf-8")).hexdigest()[:24]


def legacy_id(principal: str, entity: str, turn_id: str, direction: str) -> str:
    raw = "\0".join((str(principal), str(entity), str(turn_id), str(direction)))
    return "msg_legacy_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class ChatStore:
    def __init__(self, db_path) -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(CHAT_SCHEMA)
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(message)")}
            if "deleted_at" not in columns:
                # 舊庫補欄位（2026-11-16 私聊刪除）：0＝沒刪
                self._db.execute("ALTER TABLE message ADD COLUMN deleted_at REAL NOT NULL DEFAULT 0")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _json(value) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def is_participant(self, conversation_id: str, account_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM conversation_participant WHERE conversation_id=? AND account_id=?",
                (str(conversation_id), str(account_id))).fetchone()
        return row is not None

    def can_read_media(self, account_id: str, media_id: str) -> bool:
        """只有消息所在会话的当前参与者可读取其引用的媒体。"""
        with self._lock:
            row = self._db.execute("""
              SELECT 1
              FROM message_part AS part
              JOIN message AS message ON message.id=part.message_id
              JOIN conversation_participant AS participant
                ON participant.conversation_id=message.conversation_id
              WHERE part.kind='photo' AND part.media_id=? AND participant.account_id=?
              LIMIT 1
            """, (str(media_id), str(account_id))).fetchone()
        return row is not None

    def _require_participant(self, conversation_id: str, account_id: str) -> None:
        if not self.is_participant(conversation_id, account_id):
            # 对外不区分不存在与不可见，避免用错误码探测别人的会话。
            raise BoardError(404, "conversation_not_found")

    def ensure_direct(self, first: str, second: str, at: float) -> dict:
        conversation_id = direct_id(first, second)
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO conversation(id,kind,created_at,created_by) VALUES (?,'direct',?,?)",
                (conversation_id, float(at), str(first)))
            for account in (str(first), str(second)):
                self._db.execute(
                    "INSERT OR IGNORE INTO conversation_participant VALUES (?,?,?)",
                    (conversation_id, account, float(at)))
        return self.conversation(first, conversation_id)

    def ensure_group(self, conversation_id: str, participant_ids: list[str], *,
                     created_by: str, at: float) -> None:
        """通用群資料模型；固定房 ID 與產品成員邊界由 ChatService 決定。"""
        participants = sorted({str(account) for account in participant_ids if str(account)})
        if len(participants) < 2:
            raise ValueError("group_requires_participants")
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT kind FROM conversation WHERE id=?", (str(conversation_id),)).fetchone()
            if existing is not None and existing["kind"] != "group":
                raise BoardError(409, "conversation_kind_conflict")
            self._db.execute(
                "INSERT OR IGNORE INTO conversation(id,kind,created_at,created_by) VALUES (?,'group',?,?)",
                (str(conversation_id), float(at), str(created_by)))
            for account_id in participants:
                self._db.execute(
                    "INSERT OR IGNORE INTO conversation_participant VALUES (?,?,?)",
                    (str(conversation_id), account_id, float(at)))

    def participants(self, conversation_id: str) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT account_id FROM conversation_participant WHERE conversation_id=? ORDER BY account_id",
                (str(conversation_id),)).fetchall()
        return [str(row["account_id"]) for row in rows]

    def conversation(self, account_id: str, conversation_id: str) -> dict:
        self._require_participant(conversation_id, account_id)
        with self._lock:
            row = self._db.execute("SELECT * FROM conversation WHERE id=?",
                                   (str(conversation_id),)).fetchone()
            cursor = self._db.execute(
                "SELECT message_seq FROM read_cursor WHERE conversation_id=? AND account_id=?",
                (str(conversation_id), str(account_id))).fetchone()
        result = {"id": row["id"], "kind": row["kind"],
                  "created_at": float(row["created_at"]),
                  "participants": self.participants(conversation_id),
                  "last_message": self.latest_message(conversation_id),
                  "unread": 0}
        read_seq = int(cursor["message_seq"]) if cursor is not None else 0
        with self._lock:
            result["unread"] = int(self._db.execute(
                "SELECT COUNT(*) AS n FROM message WHERE conversation_id=? AND seq>? AND author_id<>?"
                " AND deleted_at=0",
                (str(conversation_id), read_seq, str(account_id))).fetchone()["n"])
        return result

    def list(self, account_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT conversation_id FROM conversation_participant WHERE account_id=?",
                (str(account_id),)).fetchall()
        result = [self.conversation(account_id, row["conversation_id"]) for row in rows]
        result.sort(key=lambda item: ((item["last_message"] or {}).get("created_at", 0.0),
                                      item["created_at"]), reverse=True)
        return result

    def latest_message(self, conversation_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT seq FROM message WHERE conversation_id=? ORDER BY seq DESC LIMIT 1",
                (str(conversation_id),)).fetchone()
        return self.message_by_seq(row["seq"]) if row is not None else None

    def message_by_seq(self, seq: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM message WHERE seq=?", (int(seq),)).fetchone()
        return self._message(row) if row is not None else None

    def message_by_id(self, message_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM message WHERE id=?", (str(message_id),)).fetchone()
        return self._message(row) if row is not None else None

    def _message(self, row: sqlite3.Row) -> dict:
        with self._lock:
            parts = self._db.execute(
                "SELECT * FROM message_part WHERE message_id=? ORDER BY part_index",
                (row["id"],)).fetchall()
        deleted_at = float(row["deleted_at"] or 0) if "deleted_at" in row.keys() else 0.0
        return {"id": row["id"], "seq": int(row["seq"]),
                "conversation_id": row["conversation_id"],
                # 墓碑（2026-11-16）：刪掉的訊息留著 seq 與作者，內容清空 ⇒ 分頁游標不斷、兩邊都知道「這裡刪過一則」
                "deleted": bool(deleted_at), "deleted_at": deleted_at,
                "author": {"id": row["author_id"], "kind": row["author_kind"],
                           "display": row["display"]},
                "created_at": float(row["created_at"]),
                "parts": [{"kind": part["kind"], "text": part["text"],
                           "media_id": part["media_id"], "sticker": part["sticker"],
                           "url": part["url"]} for part in parts]}

    def messages(self, account_id: str, conversation_id: str, *, before: int = 0,
                 after: int = 0, limit: int = 50) -> dict:
        self._require_participant(conversation_id, account_id)
        limit = max(1, min(200, int(limit or 50)))
        before, after = int(before or 0), int(after or 0)
        if before < 0 or after < 0:
            raise ValueError("bad_cursor")
        with self._lock:
            if after:
                rows = self._db.execute(
                    "SELECT seq FROM message WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (str(conversation_id), after, limit + 1)).fetchall()
                selected = rows[:limit]
                return {"messages": [self._message_by_seq_locked(row["seq"]) for row in selected],
                        "next_after": str(selected[-1]["seq"]) if selected else str(after),
                        "has_more": len(rows) > limit}
            if before:
                rows = self._db.execute(
                    "SELECT seq FROM message WHERE conversation_id=? AND seq<? ORDER BY seq DESC LIMIT ?",
                    (str(conversation_id), before, limit + 1)).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT seq FROM message WHERE conversation_id=? ORDER BY seq DESC LIMIT ?",
                    (str(conversation_id), limit + 1)).fetchall()
        has_more = len(rows) > limit
        selected = list(reversed(rows[:limit]))
        messages = [self.message_by_seq(row["seq"]) for row in selected]
        return {"messages": messages,
                "next_before": str(selected[0]["seq"]) if selected and has_more else "",
                "has_more": has_more}

    def _message_by_seq_locked(self, seq: int) -> dict:
        row = self._db.execute("SELECT * FROM message WHERE seq=?", (int(seq),)).fetchone()
        return self._message(row)

    def _mutation(self, actor_id: str, mutation_id: str, request: dict) -> dict | None:
        if not mutation_id:
            return None
        row = self._db.execute(
            "SELECT * FROM mutation WHERE actor_id=? AND client_mutation_id=?",
            (actor_id, mutation_id)).fetchone()
        if row is None:
            return None
        if row["kind"] != "chat.message.send" or row["request_json"] != self._json(request):
            raise BoardError(409, "mutation_conflict")
        return json.loads(row["response_json"])

    def send(self, *, actor, conversation_id: str, parts: list[dict], at: float,
             client_mutation_id: str = "", causation_id: str = "") -> dict:
        self._require_participant(conversation_id, actor.id)
        request = {"conversation_id": str(conversation_id), "parts": parts}
        with self._lock, self._db:
            cached = self._mutation(actor.id, client_mutation_id, request)
            if cached is not None:
                return {**cached, "idempotent_replay": True}
            message_id = "msg_" + secrets.token_hex(12)
            self._db.execute(
                "INSERT INTO message(id,conversation_id,author_id,author_kind,display,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (message_id, conversation_id, actor.id, actor.kind, actor.display, float(at)))
            self._insert_parts(message_id, parts)
            self._db.execute("UPDATE conversation SET last_seq=(SELECT seq FROM message WHERE id=?) WHERE id=?",
                             (message_id, conversation_id))
            participants = self.participants(conversation_id)
            conversation_kind = self._db.execute(
                "SELECT kind FROM conversation WHERE id=?", (conversation_id,)).fetchone()["kind"]
            message = self._message(self._db.execute("SELECT * FROM message WHERE id=?",
                                                     (message_id,)).fetchone())
            event_id = append_event(
                self._db, event_type="chat.message.created", at=at, actor_id=actor.id,
                actor_kind=actor.kind, entity_kind="conversation", entity_id=conversation_id,
                payload={"conversation_id": conversation_id, "conversation_kind": conversation_kind,
                         "participants": participants, "message": message},
                causation_id=causation_id, audience=participants)
            response = {"message": message, "event_id": event_id, "idempotent_replay": False}
            if client_mutation_id:
                self._db.execute(
                    "INSERT INTO mutation(actor_id,client_mutation_id,kind,request_json,response_json,created_at) "
                    "VALUES (?,?,'chat.message.send',?,?,?)",
                    (actor.id, client_mutation_id, self._json(request), self._json(response), float(at)))
            return response

    def delete_message(self, *, actor, conversation_id: str, message_id: str,
                       at: float) -> tuple[dict, list[str]]:
        """作者刪自己的一則：留墓碑（seq／作者／時間）、清空內容、記 `chat.message.deleted` 給參與者。

        回 `(墓碑, 這則原本引用的圖片 media_id)`；圖片要不要真的刪由呼叫端決定（還有沒有別處引用）。
        已經刪過 ⇒ 原樣回墓碑、不再記事件（重試冪等）。不是參與者 ⇒ 404（不洩漏會話存在）；
        是參與者但不是作者 ⇒ 403。
        """
        self._require_participant(conversation_id, actor.id)
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM message WHERE id=? AND conversation_id=?",
                                   (str(message_id), str(conversation_id))).fetchone()
            if row is None:
                raise BoardError(404, "message_not_found")
            if row["author_id"] != actor.id:
                raise BoardError(403, "not_author")
            if float(row["deleted_at"] or 0):
                return self._message(row), []
            media = [str(part["media_id"]) for part in self._db.execute(
                "SELECT media_id FROM message_part WHERE message_id=? AND kind='photo' AND media_id<>''",
                (str(message_id),)).fetchall()]
            self._db.execute("DELETE FROM message_part WHERE message_id=?", (str(message_id),))
            self._db.execute("UPDATE message SET deleted_at=? WHERE id=?", (float(at), str(message_id)))
            tombstone = self._message(self._db.execute("SELECT * FROM message WHERE id=?",
                                                       (str(message_id),)).fetchone())
            participants = self.participants(conversation_id)
            kind = self._db.execute("SELECT kind FROM conversation WHERE id=?",
                                    (str(conversation_id),)).fetchone()["kind"]
            append_event(
                self._db, event_type="chat.message.deleted", at=at, actor_id=actor.id,
                actor_kind=actor.kind, entity_kind="conversation", entity_id=str(conversation_id),
                payload={"conversation_id": str(conversation_id), "conversation_kind": kind,
                         "participants": participants, "message": tombstone},
                audience=participants)
        return tombstone, list(dict.fromkeys(media))

    def media_referenced(self, media_id: str) -> bool:
        """還有沒有任何一則訊息引用這張圖（刪掉的訊息已經清掉 part，不算）。"""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM message_part WHERE kind='photo' AND media_id=? LIMIT 1",
                (str(media_id),)).fetchone()
        return row is not None

    def _insert_parts(self, message_id: str, parts: list[dict]) -> None:
        for index, part in enumerate(parts):
            self._db.execute("INSERT INTO message_part VALUES (?,?,?,?,?,?,?)",
                             (message_id, index, part["kind"], part.get("text", ""),
                              part.get("media_id", ""), part.get("sticker", ""), part.get("url", "")))

    def mark_read(self, account_id: str, conversation_id: str, message_id: str, at: float,
                  actor_kind: str = "") -> dict:
        self._require_participant(conversation_id, account_id)
        with self._lock, self._db:
            row = self._db.execute("SELECT seq FROM message WHERE id=? AND conversation_id=?",
                                   (str(message_id), str(conversation_id))).fetchone()
            if row is None:
                raise BoardError(404, "message_not_found")
            before = self._db.execute(
                "SELECT message_seq FROM read_cursor WHERE conversation_id=? AND account_id=?",
                (str(conversation_id), str(account_id))).fetchone()
            self._db.execute(
                "INSERT INTO read_cursor VALUES (?,?,?,?) ON CONFLICT(conversation_id,account_id) "
                "DO UPDATE SET message_seq=MAX(message_seq,excluded.message_seq),updated_at=excluded.updated_at",
                (str(conversation_id), str(account_id), int(row["seq"]), float(at)))
            if before is None or int(row["seq"]) > int(before["message_seq"]):
                self._read_event(account_id, conversation_id, str(message_id), int(row["seq"]),
                                 at, actor_kind)
        return self.conversation(account_id, conversation_id)

    def _read_event(self, account_id: str, conversation_id: str, message_id: str,
                    seq: int, at: float, actor_kind: str = "") -> None:
        """已读游标**往前走**了才记一条（同一则重复标不记）：会话参与者看得到谁读到哪。

        为什么要有（docs/PLAN-common-ground-20261109.md §7.4）：她要知道自己说的话对方看到没有，
        才不会把对方没看过的话当成共同前提，也不会在对方还没读时叠着发。
        """
        participants = self.participants(conversation_id)
        kind = self._db.execute("SELECT kind FROM conversation WHERE id=?",
                                (str(conversation_id),)).fetchone()["kind"]
        append_event(
            self._db, event_type="chat.read.updated", at=at, actor_id=str(account_id),
            actor_kind=str(actor_kind or ""), entity_kind="conversation",
            entity_id=str(conversation_id),
            payload={"conversation_id": str(conversation_id), "conversation_kind": kind,
                     "account_id": str(account_id), "message_id": message_id,
                     "message_seq": int(seq), "read_at": float(at)},
            audience=participants)

    def import_legacy(self, *, principal: str, entity: str, turn_id: str, direction: str,
                      author, parts: list[dict], at: float, imported_at: float) -> tuple[dict, bool]:
        conversation_id = direct_id(principal, entity)
        message_id = legacy_id(principal, entity, turn_id, direction)
        with self._lock, self._db:
            found = self._db.execute(
                "SELECT message_id FROM legacy_turn_message WHERE principal=? AND entity=? AND turn_id=? AND direction=?",
                (principal, entity, turn_id, direction)).fetchone()
            if found is not None:
                return self.message_by_id(found["message_id"]), False
            self._db.execute(
                "INSERT OR IGNORE INTO conversation(id,kind,created_at,created_by) VALUES (?,'direct',?,?)",
                (conversation_id, float(at), principal))
            for account in (principal, entity):
                self._db.execute("INSERT OR IGNORE INTO conversation_participant VALUES (?,?,?)",
                                 (conversation_id, account, float(at)))
            self._db.execute(
                "INSERT INTO message(id,conversation_id,author_id,author_kind,display,created_at) VALUES (?,?,?,?,?,?)",
                (message_id, conversation_id, author.id, author.kind, author.display, float(at)))
            self._insert_parts(message_id, parts)
            self._db.execute("INSERT INTO legacy_turn_message VALUES (?,?,?,?,?,?)",
                             (principal, entity, turn_id, direction, message_id, float(imported_at)))
            self._db.execute("UPDATE conversation SET last_seq=(SELECT seq FROM message WHERE id=?) WHERE id=?",
                             (message_id, conversation_id))
            return self.message_by_id(message_id), True


__all__ = ["CHAT_SCHEMA", "ChatStore", "direct_id", "legacy_id"]
