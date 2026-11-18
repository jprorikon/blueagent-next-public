#!/usr/bin/env python3
"""私聊／群聊刪自己的一則（PENDING B.App.5 的 S4-d，2026-11-16）。

判據：
  ① 作者刪自己的 ⇒ 留墓碑（同 id／seq／作者，`deleted=true`、內容清空），歷史分頁照常；
  ② 參與者會收到 `chat.message.deleted`（只有參與者），外人讀不到；
  ③ 不是作者 ⇒ 403；不是參與者 ⇒ 404（不洩漏會話存在）；訊息不在這個會話 ⇒ 404；
  ④ 重刪冪等：回同一個墓碑，不再記第二條事件；
  ⑤ 墓碑不算未讀；會話列表的最後一則是墓碑；
  ⑥ 帶的圖片：自己上傳、別處沒再用 ⇒ 連檔案一起刪，對方再也讀不到；
     還被別的訊息或公共區帖引用 ⇒ 不刪；
  ⑦ 舊庫（沒有 `deleted_at` 欄）開起來會自動補上；
  ⑧ HTTP：`DELETE /v1/conversations/{id}/messages/{message_id}`（這組要能綁本機埠，沙箱裡跑不了）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import sqlite3
import tempfile
import unittest

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.config import AppConfig
from app.server.services import AppServices
from app.server.social.models import BoardError

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
ACCOUNTS = [("owner", "owner", "主人", "owner-token"), ("aster", "entity", "アスター", "aster-token"),
            ("nori", "entity", "ノリ", "nori-token"), ("acct_guest", "guest", "輝石", "guest-token")]


def write_accounts(path: pathlib.Path) -> None:
    path.write_text(json.dumps({"schema_version": 1, "accounts": [
        {"id": identity, "kind": kind, "display": display,
         "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
        for identity, kind, display, token in ACCOUNTS]}), encoding="utf-8")


class ChatDeleteCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-chat-delete-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        write_accounts(self.root / "accounts.json")
        self.services = AppServices(AppConfig("127.0.0.1", 0, self.root / "data",
                                              self.root / "accounts.json"))
        self.addCleanup(self.services.close)
        self.owner = self.actor("owner")
        self.aster = self.actor("aster")
        self.chat = self.services.chat
        self.cid = self.chat.open_direct(self.owner, "aster")["id"]

    def actor(self, account_id: str):
        return self.services.accounts.get(account_id).actor()

    def say(self, actor, text="こんにちは", cid=None, parts=None) -> dict:
        return self.chat.send(actor, cid or self.cid, text=text, parts=parts)["message"]

    def photo(self, actor) -> str:
        return self.services.media.upload(actor, PNG, "image/png")["id"]

    def events(self, account_id: str, kind: str) -> list[dict]:
        rows = self.services.events.list(account_id=account_id, limit=200)["events"]
        return [row for row in rows if row["type"] == kind]


class TestDelete(ChatDeleteCase):
    def test_author_deletes_and_leaves_a_tombstone(self):
        first = self.say(self.owner, "一")
        second = self.say(self.owner, "二")
        self.say(self.aster, "三")
        result = self.chat.delete(self.owner, self.cid, second["id"])
        tomb = result["message"]
        self.assertEqual((tomb["id"], tomb["seq"], tomb["author"]["id"]),
                         (second["id"], second["seq"], "owner"))
        self.assertTrue(tomb["deleted"])
        self.assertGreater(tomb["deleted_at"], 0)
        self.assertEqual(tomb["parts"], [])
        history = self.chat.messages(self.aster, self.cid)["messages"]
        self.assertEqual([row["id"] for row in history], [first["id"], second["id"], history[2]["id"]],
                         "墓碑留在原位，分頁游標不斷")
        self.assertEqual([row["deleted"] for row in history], [False, True, False])

    def test_participants_get_the_event_and_outsiders_do_not(self):
        message = self.say(self.owner)
        self.chat.delete(self.owner, self.cid, message["id"])
        for account in ("owner", "aster"):
            rows = self.events(account, "chat.message.deleted")
            self.assertEqual(len(rows), 1, account)
            self.assertEqual(rows[0]["payload"]["message"]["id"], message["id"])
            self.assertTrue(rows[0]["payload"]["message"]["deleted"])
        self.assertEqual(self.events("nori", "chat.message.deleted"), [])

    def test_permissions(self):
        message = self.say(self.owner)
        with self.assertRaises(BoardError) as caught:
            self.chat.delete(self.aster, self.cid, message["id"])
        self.assertEqual((caught.exception.status, caught.exception.code), (403, "not_author"))
        with self.assertRaises(BoardError) as caught:
            self.chat.delete(self.actor("nori"), self.cid, message["id"])
        self.assertEqual(caught.exception.status, 404, "外人不能知道這個會話存在")
        other = self.chat.open_direct(self.owner, "nori")["id"]
        with self.assertRaises(BoardError) as caught:
            self.chat.delete(self.owner, other, message["id"])
        self.assertEqual(caught.exception.status, 404, "訊息不在這個會話")
        self.assertFalse(self.chat.messages(self.owner, self.cid)["messages"][0]["deleted"])

    def test_deleting_twice_is_idempotent(self):
        message = self.say(self.owner)
        first = self.chat.delete(self.owner, self.cid, message["id"])
        second = self.chat.delete(self.owner, self.cid, message["id"])
        self.assertEqual(first["message"], second["message"])
        self.assertEqual(len(self.events("aster", "chat.message.deleted")), 1)

    def test_tombstone_is_not_unread_and_shows_as_last_message(self):
        message = self.say(self.owner)
        self.assertEqual(self.services.chat_store.conversation("aster", self.cid)["unread"], 1)
        self.chat.delete(self.owner, self.cid, message["id"])
        row = self.services.chat_store.conversation("aster", self.cid)
        self.assertEqual(row["unread"], 0)
        self.assertTrue(row["last_message"]["deleted"])


class TestMediaPurge(ChatDeleteCase):
    def readable(self, actor, media_id: str) -> bool:
        try:
            self.services.media.read(actor, media_id)
        except BoardError:
            return False
        return True

    def test_unreferenced_photo_is_deleted_with_the_message(self):
        media_id = self.photo(self.owner)
        message = self.say(self.owner, parts=[{"kind": "photo", "media_id": media_id}])
        self.assertTrue(self.readable(self.aster, media_id))
        result = self.chat.delete(self.owner, self.cid, message["id"])
        self.assertEqual(result["media_deleted"], [media_id])
        self.assertFalse(self.readable(self.aster, media_id))
        self.assertFalse(self.readable(self.owner, media_id), "列與檔案都沒了")
        self.assertEqual(list((self.root / "data" / "media").glob(media_id + ".*")), [])

    def test_photo_still_used_elsewhere_is_kept(self):
        media_id = self.photo(self.owner)
        first = self.say(self.owner, parts=[{"kind": "photo", "media_id": media_id}])
        self.say(self.owner, parts=[{"kind": "photo", "media_id": media_id}])
        result = self.chat.delete(self.owner, self.cid, first["id"])
        self.assertEqual(result["media_deleted"], [])
        self.assertTrue(self.readable(self.aster, media_id), "第二則還在用")

    def test_photo_used_by_a_board_post_is_kept(self):
        media_id = self.photo(self.owner)
        message = self.say(self.owner, parts=[{"kind": "photo", "media_id": media_id}])
        self.services.board.post(self.owner, "公共区にも貼った", media=[media_id])
        result = self.chat.delete(self.owner, self.cid, message["id"])
        self.assertEqual(result["media_deleted"], [])
        self.assertTrue(self.readable(self.owner, media_id))

    def test_purge_refuses_someone_elses_media(self):
        media_id = self.photo(self.aster)
        self.assertFalse(self.services.media.purge(media_id, "owner"))
        self.assertTrue(self.readable(self.aster, media_id))


class TestOldDatabase(unittest.TestCase):
    def test_missing_column_is_added(self):
        from app.server.chat.store import ChatStore

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "old.db"
            db = sqlite3.connect(path)
            db.executescript("""
              CREATE TABLE message (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                conversation_id TEXT NOT NULL, author_id TEXT NOT NULL, author_kind TEXT NOT NULL,
                display TEXT NOT NULL, created_at REAL NOT NULL);
              INSERT INTO message(id,conversation_id,author_id,author_kind,display,created_at)
                VALUES ('msg_old','cnv','owner','owner','主人',1.0);
            """)
            db.commit()
            db.close()
            store = ChatStore(path)
            self.addCleanup(store.close)
            self.assertFalse(store.message_by_id("msg_old")["deleted"])


class TestHttpRoute(unittest.TestCase):
    """真的打 HTTP（要能綁本機埠）。"""

    def setUp(self) -> None:
        import test_app_server_chat as harness

        harness.start_app_server(self)
        self.request = lambda *args, **kwargs: harness.chat_request(self.base, *args, **kwargs)

    def test_delete_route(self):
        status, opened = self.request("POST", "/v1/conversations/direct", {"account_id": "aster"})
        self.assertEqual(status, 200)
        cid = opened["conversation"]["id"]
        status, sent = self.request("POST", f"/v1/conversations/{cid}/messages", {"text": "消す"})
        mid = sent["message"]["id"]
        status, body = self.request("DELETE", f"/v1/conversations/{cid}/messages/{mid}",
                                    token="aster-token")
        self.assertEqual((status, body["error"]), (403, "not_author"))
        status, body = self.request("DELETE", f"/v1/conversations/{cid}/messages/{mid}")
        self.assertEqual(status, 200)
        self.assertTrue(body["message"]["deleted"])
        status, page = self.request("GET", f"/v1/conversations/{cid}/messages", token="aster-token")
        self.assertTrue(page["messages"][-1]["deleted"])
        status, _ = self.request("DELETE", f"/v1/conversations/{cid}/messages/msg_nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
