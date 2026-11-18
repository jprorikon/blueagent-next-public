#!/usr/bin/env python3
"""S4-d：App Server 自有媒体、会话／公共区 ACL、缩图与 MCP 元数据。"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.config import AppConfig
from app.server.http import make_server
from app.server.media import MAX_BYTES

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class AppServerMediaCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-app-media-")
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        accounts = root / "accounts.json"
        rows = [("owner", "owner", "主人"), ("aster", "entity", "アスター"), ("nori", "entity", "ノリ")]
        accounts.write_text(json.dumps({"schema_version": 1, "accounts": [
            {"id": identity, "kind": kind, "display": display,
             "token_sha256": hashlib.sha256((identity + "-token").encode()).hexdigest()}
            for identity, kind, display in rows]}), encoding="utf-8")
        self.server = make_server(AppConfig("127.0.0.1", 0, root / "data", accounts))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.thread.join, 2.0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, token="owner-token", headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token}" if token else "",
                     "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                content = response.read()
                return response.status, response.headers, content
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def json_request(self, *args, **kwargs):
        status, headers, raw = self.request(*args, **kwargs)
        return status, json.loads(raw) if raw else None

    def upload(self, token="owner-token", data=PNG, content_type="image/png"):
        status, body = self.json_request("POST", "/v1/media", token=token, raw=data,
                                         headers={"Content-Type": content_type})
        self.assertEqual(status, 200, body)
        return body

    def direct(self):
        status, body = self.json_request("POST", "/v1/conversations/direct", {"account_id": "aster"})
        self.assertEqual(status, 200, body)
        return body["conversation"]["id"]

    def test_upload_list_content_type_limit_and_etag(self):
        item = self.upload()
        self.assertTrue(item["id"].startswith("m"))
        self.assertEqual((item["bytes"], item["mime"], item["owner_id"]),
                         (len(PNG), "image/png", "owner"))
        self.assertNotIn(str(pathlib.Path(self.tmp.name) / "data"), repr(item))
        status, listed = self.json_request("GET", "/v1/media", token="aster-token")
        self.assertEqual((status, listed["total"], listed["items"]), (200, 0, []))
        status, listed = self.json_request("GET", "/v1/media")
        self.assertEqual([row["id"] for row in listed["items"]], [item["id"]])

        status, headers, raw = self.request("GET", item["url"])
        self.assertEqual((status, raw), (200, PNG))
        self.assertEqual(headers.get_content_type(), "image/png")
        tag = headers["ETag"]
        status, headers, raw = self.request("GET", item["url"], headers={"If-None-Match": tag})
        self.assertEqual((status, raw), (304, b""))
        status, headers, raw = self.request("GET", item["url"] + "?w=192")
        self.assertEqual(status, 200)
        self.assertTrue(headers["ETag"].strip('"').endswith("-w192"))

        status, denied = self.json_request("POST", "/v1/media", raw=b"plain",
                                           headers={"Content-Type": "text/plain"})
        self.assertEqual((status, denied["error"]), (415, "unsupported_type"))
        status, large = self.json_request("POST", "/v1/media", raw=b"x" * (MAX_BYTES + 1),
                                          headers={"Content-Type": "image/png"})
        self.assertEqual((status, large["error"], large["limit"]),
                         (413, "image_too_large", MAX_BYTES))

    def test_chat_acl_and_send_requires_owned_media(self):
        item = self.upload()
        cid = self.direct()
        for parts in ([{"kind": "photo", "media_id": "mdeadbeef"}],
                      [{"kind": "photo", "media_id": item["id"]}],  # aster 不拥有
                      [{"kind": "photo", "url": "https://example.invalid/private.png"}]):
            token = "aster-token" if len(parts[0].get("media_id", "")) > 8 else "owner-token"
            status, denied = self.json_request("POST", f"/v1/conversations/{cid}/messages",
                                               {"parts": parts}, token=token)
            self.assertEqual((status, denied["error"]), (400, "unknown_media"), parts)
        status, sent = self.json_request("POST", f"/v1/conversations/{cid}/messages",
                                         {"parts": [{"kind": "photo", "media_id": item["id"]},
                                                    {"kind": "text", "text": "見て"}]})
        self.assertEqual(status, 200, sent)
        for token in ("owner-token", "aster-token"):
            status, _, raw = self.request("GET", item["url"], token=token)
            self.assertEqual((status, raw), (200, PNG))
        status, denied = self.json_request("GET", item["url"], token="nori-token")
        self.assertEqual((status, denied["error"]), (404, "not_found"))

        status, reply = self.json_request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "media.get", "arguments": {"id": item["id"]}}}, token="aster-token")
        result = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(result["media"]["url"], item["url"])

    def test_public_post_grants_access_and_delete_revokes_it(self):
        item = self.upload()
        status, posted = self.json_request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "post", "arguments": {"text": "写真", "media": [item["id"]]}}})
        self.assertEqual(status, 200, posted)
        post = json.loads(posted["result"]["content"][0]["text"])["post"]
        status, _, raw = self.request("GET", item["url"], token="aster-token")
        self.assertEqual((status, raw), (200, PNG))
        status, deleted = self.json_request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "delete", "arguments": {"id": post["id"]}}})
        self.assertEqual(status, 200, deleted)
        status, denied = self.json_request("GET", item["url"], token="aster-token")
        self.assertEqual((status, denied["error"]), (404, "not_found"))
        status, _, raw = self.request("GET", item["url"])
        self.assertEqual((status, raw), (200, PNG), "上传者不受删帖影响")

    def test_legacy_media_migration_is_dry_idempotent_and_never_guesses_owner(self):
        from app.server.media_migration import migrate_media

        legacy = pathlib.Path(self.tmp.name) / "legacy-media"
        legacy.mkdir()
        rows = [
            {"id": "m0123456789abcdef", "ext": ".png", "bytes": len(PNG), "at": 123.5,
             "principal": "owner", "entity": "aster", "turn_id": "t1", "name": "one"},
            {"id": "mfedcba9876543210", "ext": ".png", "bytes": len(PNG), "at": 124.5,
             "principal": "aster", "entity": "owner", "turn_id": "t2", "name": "two"},
            {"id": "m1111111111111111", "ext": ".png", "bytes": len(PNG), "at": 125.5,
             "principal": "unknown"},
        ]
        for row in rows:
            (legacy / (row["id"] + row["ext"])).write_bytes(PNG)
        index = legacy / "index.jsonl"
        index.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        original = index.read_bytes()
        dry = migrate_media(index, self.server.services.media, self.server.services.accounts)
        self.assertTrue(dry["dry_run"])
        self.assertEqual((dry["planned"], dry["conflicts"], dry["bytes"]),
                         (2, 1, len(PNG) * 2), dry)
        self.assertEqual(index.read_bytes(), original)
        first = migrate_media(index, self.server.services.media, self.server.services.accounts,
                              apply=True)
        self.assertEqual((first["imported"], first["conflicts"]), (2, 1), first)
        second = migrate_media(index, self.server.services.media, self.server.services.accounts,
                               apply=True)
        self.assertEqual((second["imported"], second["idempotent_replays"],
                          second["conflicts"]), (0, 2, 1), second)
        status, _, raw = self.request("GET", "/v1/media/m0123456789abcdef")
        self.assertEqual((status, raw), (200, PNG))
        metadata = self.server.services.media._row("m0123456789abcdef")
        self.assertEqual((metadata["at"], metadata["turn_id"]), (123.5, "t1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
