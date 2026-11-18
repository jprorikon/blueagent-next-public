"""公共区 SQLite 索引与互动状态；手动帖／回复正文另存按月 JSONL 原文。"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
import time

from app.server.events import EVENT_SCHEMA, append_event
from app.server.social.models import BoardError, Post

_SCHEMA = EVENT_SCHEMA + """
CREATE TABLE IF NOT EXISTS post (
  id TEXT PRIMARY KEY, at REAL NOT NULL, author_kind TEXT NOT NULL,
  author_id TEXT NOT NULL, display TEXT NOT NULL, origin TEXT NOT NULL,
  source_ref TEXT, body TEXT NOT NULL, media TEXT, kind TEXT NOT NULL DEFAULT 'post',
  visibility TEXT NOT NULL DEFAULT 'public', parent_id TEXT,
  deleted_at REAL, deleted_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS post_source ON post(source_ref) WHERE source_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS post_feed ON post(at DESC, id DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS post_replies ON post(parent_id, at, id) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS reaction (
  post_id TEXT NOT NULL, actor_id TEXT NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL,
  PRIMARY KEY (post_id, actor_id, kind)
);
CREATE TABLE IF NOT EXISTS counter (
  post_id TEXT PRIMARY KEY, likes INTEGER DEFAULT 0, replies INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mutation (
  actor_id TEXT NOT NULL, client_mutation_id TEXT NOT NULL, kind TEXT NOT NULL,
  request_json TEXT NOT NULL, response_json TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY (actor_id, client_mutation_id)
);
"""


class BoardStore:
    def __init__(self, data_root) -> None:
        self.data_root = pathlib.Path(data_root)
        self.root = self.data_root / "board"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "board.db"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _row(post: Post) -> tuple:
        row = post.to_row()
        return (row["id"], row["at"], row["author_kind"], row["author_id"], row["display"],
                row["origin"], row["source_ref"], row["body"], json.dumps(row["media"], ensure_ascii=False),
                row["kind"], row["visibility"], row["parent_id"], row["deleted_at"], row["deleted_by"])

    @staticmethod
    def _json(value: dict) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def mutation(self, actor_id: str, mutation_id: str, kind: str, request: dict) -> dict | None:
        """同一 key 只能重放同一请求；命中时返回首次提交缓存的响应。"""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM mutation WHERE actor_id=? AND client_mutation_id=?",
                (actor_id, mutation_id)).fetchone()
        if row is None:
            return None
        if row["kind"] != kind or row["request_json"] != self._json(request):
            raise BoardError(409, "mutation_conflict")
        return json.loads(row["response_json"])

    def _remember_mutation(self, mutation: dict | None) -> None:
        if mutation is None:
            return
        self._db.execute("""
          INSERT INTO mutation(actor_id,client_mutation_id,kind,request_json,response_json,created_at)
          VALUES (?,?,?,?,?,?)
        """, (mutation["actor_id"], mutation["id"], mutation["kind"],
              self._json(mutation["request"]), self._json(mutation["response"]),
              float(mutation["at"])))

    def upsert_projection(self, post: Post) -> None:
        with self._lock, self._db:
            self._db.execute("""
              INSERT INTO post VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET at=excluded.at, display=excluded.display,
                body=excluded.body, media=excluded.media, kind=excluded.kind,
                visibility=excluded.visibility, source_ref=excluded.source_ref
            """, self._row(post))

    def insert_projection(self, posts: list[Post]) -> int:
        with self._lock:
            for post in posts:
                self.upsert_projection(post)
        return len(posts)

    def create_manual(self, post: Post, mutation: dict | None = None,
                      notice: dict | None = None) -> None:
        """原文、索引、事件和首次幂等响应按既有顺序提交；失败仍可由 rebuild 收回。

        `notice`（B.App.8）：帖主是人類時由 service 給 ⇒ 同一交易追加一條只給帖主的 `notice.created`。
        """
        row = post.to_row()
        name = time.strftime("%Y-%m.jsonl", time.localtime(post.at))
        with self._lock:
            with (self.root / name).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
            with self._db:
                self._db.execute("INSERT INTO post VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 self._row(post))
                if post.parent_id:
                    self._db.execute("""
                      INSERT INTO counter(post_id, likes, replies) VALUES (?,0,1)
                      ON CONFLICT(post_id) DO UPDATE SET replies=replies+1
                    """, (post.parent_id,))
                append_event(
                    self._db, event_type="reply.created" if post.parent_id else "post.created",
                    at=post.at, actor_id=post.author_id, actor_kind=post.author_kind,
                    entity_kind="post", entity_id=post.id, payload={"post": row})
                self._append_notice(notice, at=post.at, actor_id=post.author_id,
                                    actor_kind=post.author_kind)
                self._remember_mutation(mutation)

    def restore_manual(self, post: Post) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute("INSERT OR IGNORE INTO post VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      self._row(post))
            if cursor.rowcount and post.parent_id:
                self._db.execute("""
                  INSERT INTO counter(post_id, likes, replies) VALUES (?,0,1)
                  ON CONFLICT(post_id) DO UPDATE SET replies=replies+1
                """, (post.parent_id,))
            return bool(cursor.rowcount)

    def get(self, post_id: str) -> Post | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM post WHERE id=?", (str(post_id),)).fetchone()
        return Post.from_row(dict(row)) if row is not None else None

    def manual_posts(self) -> list[Post]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM post WHERE origin='manual'").fetchall()
        return [Post.from_row(dict(row)) for row in rows]

    def feed_posts(self) -> list[Post]:
        """独立平台的持久快照；回复只经详情读取，不混入一级时间线。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM post WHERE parent_id IS NULL "
                "AND origin IN ('manual','projection')").fetchall()
        return [Post.from_row(dict(row)) for row in rows]

    def by_source_ref(self, source_ref: str) -> Post | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM post WHERE source_ref=?", (str(source_ref),)).fetchone()
        return Post.from_row(dict(row)) if row is not None else None

    def replies(self, parent_id: str) -> list[Post]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM post WHERE parent_id=? AND deleted_at IS NULL ORDER BY at,id",
                (str(parent_id),)).fetchall()
        return [Post.from_row(dict(row)) for row in rows]

    def projection_states(self) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id,deleted_at,deleted_by FROM post WHERE origin='projection'").fetchall()
        return {row["id"]: {"deleted_at": float(row["deleted_at"] or 0.0),
                            "deleted_by": str(row["deleted_by"] or "")} for row in rows}

    def counters(self, post_ids: list[str], actor_id: str) -> dict[str, dict]:
        if not post_ids:
            return {}
        marks = ",".join("?" for _ in post_ids)
        with self._lock:
            counts = self._db.execute(
                f"SELECT * FROM counter WHERE post_id IN ({marks})", post_ids).fetchall()
            liked = self._db.execute(
                f"SELECT post_id FROM reaction WHERE actor_id=? AND kind='like' "
                f"AND post_id IN ({marks})", [actor_id, *post_ids]).fetchall()
        result = {post_id: {"likes": 0, "replies": 0, "liked": False} for post_id in post_ids}
        for row in counts:
            result[row["post_id"]].update(likes=int(row["likes"]), replies=int(row["replies"]))
        for row in liked:
            result[row["post_id"]]["liked"] = True
        return result

    def like(self, post_id: str, actor_id: str, at: float, actor_kind: str = "",
             notice: dict | None = None) -> bool:
        """回 True 仅表示这次真的新增；重复点赞不产生第二条事件（也不再通知帖主）。"""
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO reaction(post_id,actor_id,kind,at) VALUES (?,?,'like',?)",
                (post_id, actor_id, float(at)))
            if not cursor.rowcount:
                return False
            self._db.execute("""
              INSERT INTO counter(post_id,likes,replies) VALUES (?,1,0)
              ON CONFLICT(post_id) DO UPDATE SET likes=likes+1
            """, (post_id,))
            append_event(self._db, event_type="post.liked", at=at,
                         actor_id=actor_id, actor_kind=actor_kind,
                         entity_kind="post", entity_id=post_id,
                         payload={"post_id": post_id})
            self._append_notice(notice, at=at, actor_id=actor_id, actor_kind=actor_kind)
            return True

    def _append_notice(self, notice: dict | None, *, at: float, actor_id: str,
                       actor_kind: str) -> None:
        """人類帖主的通知（owner 2026-11-16 B.App.8）：只有帖主讀得到，App 內提醒條與系統推送都看這一條。"""
        if not notice or not str(notice.get("recipient") or ""):
            return
        payload = dict(notice.get("payload") or {})
        append_event(self._db, event_type="notice.created", at=at, actor_id=actor_id,
                     actor_kind=actor_kind, entity_kind="post",
                     entity_id=str(payload.get("post_id") or ""), payload=payload,
                     audience=[str(notice["recipient"])])

    def soft_delete(self, post_id: str, actor_id: str, at: float, actor_kind: str = "") -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE post SET deleted_at=?,deleted_by=? WHERE id=?",
                             (float(at), actor_id, post_id))
            append_event(self._db, event_type="post.deleted", at=at,
                         actor_id=actor_id, actor_kind=actor_kind,
                         entity_kind="post", entity_id=post_id,
                         payload={"post_id": post_id})

    def recent_manual_count(self, author_id: str, since: float) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM post WHERE origin='manual' AND author_id=? AND at>=?",
                (author_id, float(since))).fetchone()
        return int(row["n"])

    def canonical(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT id,at,body,source_ref FROM post ORDER BY id").fetchall()
        return [{"id": row["id"], "at": round(float(row["at"]), 3),
                 "body": row["body"], "source_ref": row["source_ref"]} for row in rows]


__all__ = ["BoardStore"]
