#!/usr/bin/env python3
"""App 側快取＋線上雜湊（owner 2026-10-29：「圖片、表情包和聊天記錄每次都要重新下載，
能不能採用 app 側快取，對比線上快取的雜湊決定是不是要重新同步」）。

判據：
  ① 表情包清單每張附 `hash`（＝那張圖的 ETag）、整包附 `version`（＝清單的 ETag）；圖換了雜湊跟著變；
  ② 圖（表情包／相冊）回 `ETag`；帶同一個雜湊來問 ⇒ 304、沒有本文；
  ③ `GET /api/turns` 附 `digest`（＝ETag）；沒新回合 ⇒ 304，多了一輪 ⇒ 雜湊變、回 200；
  ④ 304 也不鬆動權限：沒 token 照樣 401、別人的圖照樣 404。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403

from agent.brain.media import digest
from agent.brain.media.store import MediaStore
from agent.server import api as api_module
from test_stickers import OWNER, PNG, StickerTurnCase


class TestDigest(unittest.TestCase):
    def test_json_tag_ignores_key_order(self):
        self.assertEqual(digest.json_tag({"a": 1, "b": [1, 2]}), digest.json_tag({"b": [1, 2], "a": 1}))
        self.assertNotEqual(digest.json_tag([1, 2]), digest.json_tag([2, 1]))
        self.assertEqual(len(digest.content_tag(b"x")), digest.LENGTH)

    def test_shared_vectors_with_the_unity_client(self):
        """端末（`SyncCache.ContentTag`）回應沒帶 ETag 時自己算雜湊 ⇒ 兩邊必須同一個算法。
        同一組向量也釘在 `SyncCacheTest.本機雜湊跟伺服器同一個算法`。"""
        self.assertEqual(digest.content_tag(b""), "e3b0c44298fc1c14")
        self.assertEqual(digest.content_tag("スタンプ".encode("utf-8")), "57a0d8fbec6f9b84")
        self.assertEqual(digest.content_tag(bytes(range(256))), "40aff2e9d2d8922e")

    def test_if_none_match_forms(self):
        tag = digest.content_tag(b"x")
        for header in (f'"{tag}"', f'W/"{tag}"', f'"zzz", "{tag}"', tag, "*"):
            self.assertTrue(digest.matches(header, tag), header)
        for header in ("", '"other"'):
            self.assertFalse(digest.matches(header, tag), header)
        self.assertFalse(digest.matches(f'"{tag}"', ""))


class TestStickerHashes(StickerTurnCase):
    def test_each_sticker_carries_its_file_hash(self):
        status, data = api_module.list_stickers(self.state, {"entity": ["mira"]}, auth=OWNER)
        self.assertEqual(status, 200)
        for row in data["stickers"]:
            self.assertEqual(row["hash"], digest.content_tag(PNG))
        self.assertEqual(data["version"], digest.json_tag(data["stickers"]))

    def test_hash_and_version_follow_the_file(self):
        catalog = self.state.app.stickers
        _, before = api_module.list_stickers(self.state, {"entity": ["mira"]}, auth=OWNER)
        path = catalog.path_of(catalog.get("mira", "smile"))
        path.write_bytes(PNG + b"\0")
        _, after = api_module.list_stickers(self.state, {"entity": ["mira"]}, auth=OWNER)
        self.assertNotEqual(before["version"], after["version"])
        smile = next(row for row in after["stickers"] if row["id"] == "smile")
        self.assertEqual(smile["hash"], digest.content_tag(PNG + b"\0"))


class TestTurnDigest(StickerTurnCase):
    def test_digest_moves_only_when_turns_change(self):
        outbox = self.state.app.outbox
        outbox.record(turn_id="t1", principal="owner", entity="mira", text="a", source="phone:owner")
        query = {"entity": ["mira"], "source": ["phone,api"]}
        _, first = api_module.list_turns(self.state, query, auth=OWNER)
        _, again = api_module.list_turns(self.state, query, auth=OWNER)
        self.assertEqual(first["digest"], again["digest"])
        self.assertEqual(first["digest"], digest.json_tag(first["turns"]))
        # 別的角色多了一輪：這一窗沒變 ⇒ 雜湊不變（`stats` 變了也不算）
        outbox.record(turn_id="t2", principal="owner", entity="aster", text="b", source="phone:owner")
        _, other = api_module.list_turns(self.state, query, auth=OWNER)
        self.assertEqual(first["digest"], other["digest"])
        outbox.record(turn_id="t3", principal="owner", entity="mira", text="c", source="phone:owner")
        _, moved = api_module.list_turns(self.state, query, auth=OWNER)
        self.assertNotEqual(first["digest"], moved["digest"])


class TestConditionalHttp(StickerTurnCase):
    def setUp(self):
        super().setUp()
        from agent.server import http as app_server
        app_server.set_state(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.token = self.state.app.auth.owner_token
        from platform_server import start_platform
        self.platform = start_platform(self, [("owner", "owner", self.token)])

    def get(self, path, *, token=None, tag=""):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if tag:
            headers["If-None-Match"] = f'"{tag}"'
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers.get("ETag", ""), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("ETag", ""), exc.read()

    def test_sticker_list_and_file(self):
        status, etag, body = self.get("/api/stickers?entity=mira", token=self.token)
        data = json.loads(body)
        self.assertEqual((status, etag), (200, f'"{data["version"]}"'))
        self.assertEqual(self.get("/api/stickers?entity=mira", token=self.token,
                                  tag=data["version"])[::2], (304, b""))
        row = data["stickers"][0]
        status, etag, body = self.get(row["url"], token=self.token)
        self.assertEqual((status, etag, body), (200, f'"{row["hash"]}"', PNG))
        self.assertEqual(self.get(row["url"], token=self.token, tag=row["hash"])[::2], (304, b""))
        self.assertEqual(self.get(row["url"], token=self.token, tag="stale")[0], 200)
        self.assertEqual(self.get(row["url"], tag=row["hash"])[0], 401, "304 不能繞過登入")

    def test_media_file(self):
        self.state.media = MediaStore(pathlib.Path(tempfile.mkdtemp(prefix="ba-media-")))
        row = self.state.media.save(PNG, ext=".png", principal="owner", entity="mira")
        self.platform.services.media.import_legacy(row, PNG, "owner")
        path = "/api/media/" + row["id"]
        status, etag, body = self.get(path, token=self.token)
        self.assertEqual((status, body), (200, PNG))
        tag = etag.strip('"')
        self.assertEqual(tag, digest.content_tag(PNG))
        self.assertEqual(self.get(path, token=self.token, tag=tag)[::2], (304, b""))
        other = self.state.media.save(PNG, ext=".png", principal="someone-else")
        self.assertEqual(self.get("/api/media/" + other["id"], token=self.token, tag=tag)[0], 404,
                         "別人的圖：雜湊對上也是 404")

    def test_turns(self):
        outbox = self.state.app.outbox
        outbox.record(turn_id="t1", principal="owner", entity="mira", text="a", source="phone:owner")
        path = "/api/turns?limit=50&entity=mira&source=phone,api"
        status, etag, body = self.get(path, token=self.token)
        data = json.loads(body)
        self.assertEqual((status, etag), (200, f'"{data["digest"]}"'))
        self.assertEqual(self.get(path, token=self.token, tag=data["digest"])[::2], (304, b""))
        outbox.record(turn_id="t2", principal="owner", entity="mira", text="b", source="phone:owner")
        status, _, body = self.get(path, token=self.token, tag=data["digest"])
        self.assertEqual(status, 200)
        self.assertEqual([row["turn_id"] for row in json.loads(body)["turns"]], ["t1", "t2"])
        self.assertEqual(self.get(path, tag=data["digest"])[0], 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
