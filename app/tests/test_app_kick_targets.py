#!/usr/bin/env python3
"""推送叫醒（`docs/PLAN-app-kick-20261108.md`）：判據、flush 時機、fire-and-forget。

判據是 owner 2026-11-08 逐條給的，這裡逐條釘住——**判據錯了，通路做得再對也沒用**。
"""

from __future__ import annotations

import http.server
import json
import pathlib
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from app.server.events import EVENT_SCHEMA, EventLog, append_event
from app.server.kicker import EventsKicker, targets_for

POST_SCHEMA = """
CREATE TABLE IF NOT EXISTS post (
  id TEXT PRIMARY KEY, author_id TEXT NOT NULL, author_kind TEXT NOT NULL
);
"""


def _db(root: pathlib.Path) -> sqlite3.Connection:
    db = sqlite3.connect(root / "events.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(EVENT_SCHEMA + POST_SCHEMA)
    return db


def _accounts(kinds: dict[str, str]):
    return SimpleNamespace(kind_of=kinds,
                           get=lambda account_id: (
                               SimpleNamespace(kind=kinds[account_id])
                               if account_id in kinds else None))


class TargetsForCase(unittest.TestCase):
    """判據表（§3）逐條。"""

    def setUp(self):
        self.posts = {"p_her": ("mira", "entity"), "p_owner": ("owner", "owner"),
                      "p_guest": ("acct_guest", "guest")}
        self.kinds = {"owner": "owner", "mira": "entity", "nori": "entity",
                      "aster": "entity", "acct_guest": "guest"}

    def targets(self, event_type, payload, actor_id="owner", actor_kind="owner"):
        return targets_for(event_type=event_type, payload=payload,
                           actor_id=actor_id, actor_kind=actor_kind,
                           lookup_post=lambda post_id: self.posts.get(post_id),
                           is_entity=lambda account_id: self.kinds.get(account_id) == "entity")

    def test_direct_message_wakes_the_entity_not_the_author(self):
        self.assertEqual(self.targets("chat.message.created",
                                      {"participants": ["owner", "mira", "acct_guest"]}),
                         ["mira"])

    def test_group_message_wakes_every_entity_in_the_room(self):
        self.assertEqual(self.targets("chat.message.created",
                                      {"participants": ["owner", "mira", "nori", "aster"]}),
                         ["mira", "nori", "aster"])

    def test_entity_speaking_does_not_wake_itself(self):
        self.assertEqual(self.targets("chat.message.created",
                                      {"participants": ["owner", "mira", "nori"]},
                                      actor_id="mira", actor_kind="entity"),
                         ["nori"])

    def test_post_created_wakes_nobody(self):
        self.assertEqual(self.targets("post.created", {"post": {"id": "p_her"}}), [])

    def test_reply_to_entity_post_wakes_the_owner_of_the_post(self):
        self.assertEqual(self.targets("reply.created", {"post": {"id": "p_her"}}), ["mira"])

    def test_reply_to_human_post_wakes_nobody_yet(self):
        # 人類通知是待辦（通道未定）⇒ 這裡刻意不叫醒任何實體。
        self.assertEqual(self.targets("reply.created", {"post": {"id": "p_owner"}}), [])
        self.assertEqual(self.targets("reply.created", {"post": {"id": "p_guest"}},
                                      actor_id="owner"), [])

    def test_like_wakes_the_post_author(self):
        self.assertEqual(self.targets("post.liked", {"post_id": "p_her"}), ["mira"])

    def test_self_delete_wakes_nobody_but_other_delete_wakes_author(self):
        self.assertEqual(self.targets("post.deleted", {"post_id": "p_her"},
                                      actor_id="mira", actor_kind="entity"), [])
        self.assertEqual(self.targets("post.deleted", {"post_id": "p_her"}, actor_id="owner"),
                         ["mira"])

    def test_location_and_mood_wake_nobody(self):
        for event_type in ("location.updated", "location.revoked", "mood.updated"):
            self.assertEqual(self.targets(event_type, {"mood": {}}), [], event_type)

    def test_missing_post_or_empty_payload_is_silent(self):
        self.assertEqual(self.targets("post.liked", {"post_id": "p_gone"}), [])
        self.assertEqual(self.targets("post.liked", {}), [])
        self.assertEqual(self.targets("chat.message.created", {}), [])


class FlushCase(unittest.TestCase):
    """`flush()` 只叫不該叫的；空佇列／未設定 URL 一律安靜。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-kick-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.db = _db(self.root)
        self.addCleanup(self.db.close)

    def _log(self, kicker):
        return EventLog(self.root / "events.db", kicker=kicker,
                        accounts=_accounts({"owner": "owner", "mira": "entity"}))

    def test_pending_events_are_woken_after_commit(self):
        kicked = []

        class Kicker:
            def enabled(self): return True
            def kick(self, entities, *, reason=""): kicked.append((list(entities), reason))

        with self.db:
            append_event(self.db, event_type="chat.message.created", at=1.0, actor_id="owner",
                         actor_kind="owner", entity_kind="conversation", entity_id="cnv",
                         payload={"participants": ["owner", "mira"]},
                         audience=["owner", "mira"])
            self.assertEqual(kicked, [], "提交前不該叫醒")
        self.assertEqual(self._log(Kicker()).flush(), 1)
        self.assertEqual(kicked, [(["mira"], "chat.message.created")])

    def test_queue_is_drained_so_a_second_flush_is_silent(self):
        kicked = []

        class Kicker:
            def enabled(self): return True
            def kick(self, entities, *, reason=""): kicked.append(entities)

        with self.db:
            append_event(self.db, event_type="post.liked", at=1.0, actor_id="owner",
                         actor_kind="owner", entity_kind="post", entity_id="p_her",
                         payload={"post_id": "p_her"})
            self.db.execute("INSERT INTO post(id,author_id,author_kind) VALUES ('p_her','mira','entity')")
        log = self._log(Kicker())
        self.assertEqual(log.flush(), 1)
        self.assertEqual(log.flush(), 0)
        self.assertEqual(len(kicked), 1)

    def test_disabled_kicker_drops_silently(self):
        with self.db:
            append_event(self.db, event_type="chat.message.created", at=1.0, actor_id="owner",
                         actor_kind="owner", entity_kind="conversation", entity_id="cnv",
                         payload={"participants": ["owner", "mira"]})
        self.assertEqual(self._log(EventsKicker("")).flush(), 0)


class TransportCase(unittest.TestCase):
    """實際送出：真的打一次 HTTP，並確認打不通時**不拋**（紅線）。"""

    def setUp(self):
        received = self.received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):                       # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                received.append((self.path, self.headers.get("X-App-Kick-Token"),
                                 json.loads(body or b"{}")))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format, *args):    # noqa: A002 - 靜音，別刷測試輸出
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/app/kick"

    def _wait(self, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline and not self.received:
            time.sleep(0.02)

    def test_posts_entities_and_reason(self):
        kicker = EventsKicker(self.url, "t0ken")
        self.addCleanup(kicker.close)
        kicker.kick(["mira", "nori"], reason="chat.message.created")
        self._wait()
        self.assertEqual(self.received, [("/api/app/kick", "t0ken",
                                          {"entities": ["mira", "nori"],
                                           "reason": "chat.message.created"})])

    def test_duplicate_entities_collapse_and_empty_is_noop(self):
        kicker = EventsKicker(self.url, "t0ken")
        self.addCleanup(kicker.close)
        kicker.kick([], reason="silent")
        kicker.kick(["mira", "mira"], reason="dup")
        self._wait()
        self.assertEqual([item[2]["entities"] for item in self.received], [["mira"]])

    def test_dead_target_does_not_raise(self):
        kicker = EventsKicker("http://127.0.0.1:1/api/app/kick", "t0ken", timeout_s=0.2)
        self.addCleanup(kicker.close)
        kicker.kick(["mira"], reason="chat.message.created")   # 紅線：不拋、不阻塞
        kicker.close()


if __name__ == "__main__":
    unittest.main()
