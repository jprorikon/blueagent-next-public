#!/usr/bin/env python3
"""C-17：App Server 持有客户端贴纸，提供认证目录、MCP、ACL 与缓存契约。"""

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

from demo_common import *  # noqa: F401,F403

from app.server.config import AppConfig
from app.server.http import make_server
from agent.adapter.app_server import AppServerClient

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class AppServerStickerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-app-stickers-")
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        assets = root / "client-assets"
        for entity in ("aster", "nori"):
            folder = assets / "stickers" / entity
            folder.mkdir(parents=True)
            (folder / "smile.png").write_bytes(PNG)
            (folder / "stickers.json").write_text(json.dumps({"stickers": [{
                "id": "smile", "file": "smile.png", "label": "にこっ",
                "meaning": entity + " の笑顔"}]}, ensure_ascii=False), encoding="utf-8")
        accounts = root / "accounts.json"
        rows = [("owner", "owner", "主人", None), ("aster", "entity", "アスター", None),
                ("nori", "entity", "ノリ", None), ("guest", "guest", "客人", ["aster"])]
        accounts.write_text(json.dumps({"schema_version": 1, "accounts": [
            {"id": identity, "kind": kind, "display": display,
             "visible_entities": visible,
             "token_sha256": hashlib.sha256((identity + "-token").encode()).hexdigest()}
            for identity, kind, display, visible in rows]}), encoding="utf-8")
        self.server = make_server(AppConfig("127.0.0.1", 0, root / "data", accounts,
                                            client_assets=assets))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.thread.join, 2.0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, token="owner-token", headers=None):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode() if body is not None else None,
            method=method, headers={"Authorization": f"Bearer {token}" if token else "",
                                    "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_catalog_content_etag_mcp_and_agent_client(self):
        status, headers, raw = self.request("GET", "/v1/stickers?entity=aster")
        payload = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(payload["entity"], "aster")
        self.assertEqual(len(payload["stickers"]), 1)
        row = payload["stickers"][0]
        self.assertEqual(row["meaning"], "aster の笑顔")
        self.assertEqual(row["url"], "/v1/stickers/aster/smile")
        self.assertTrue(row["hash"])
        self.assertNotIn("file", row)
        self.assertNotIn(str(pathlib.Path(self.tmp.name)), repr(payload))
        self.assertEqual(headers["ETag"], '"' + payload["version"] + '"')
        status, _, raw = self.request("GET", "/v1/stickers?entity=aster",
                                      headers={"If-None-Match": headers["ETag"]})
        self.assertEqual((status, raw), (304, b""))

        status, headers, raw = self.request("GET", row["url"])
        self.assertEqual((status, raw), (200, PNG))
        self.assertEqual(headers.get_content_type(), "image/png")
        status, _, raw = self.request("GET", row["url"],
                                      headers={"If-None-Match": headers["ETag"]})
        self.assertEqual((status, raw), (304, b""))
        status, headers, raw = self.request("GET", row["url"] + "?w=192")
        self.assertEqual(status, 200)
        self.assertTrue(headers["ETag"].strip('"').endswith("-w192"))

        status, _, raw = self.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "sticker.list", "arguments": {}}}, token="aster-token")
        self.assertEqual(status, 200)
        result = json.loads(json.loads(raw)["result"]["content"][0]["text"])
        self.assertEqual(result["stickers"][0]["meaning"], "aster の笑顔")
        status, _, raw = self.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token="aster-token")
        self.assertIn("sticker.list", [tool["name"] for tool in
                                      json.loads(raw)["result"]["tools"]])

        client = AppServerClient(self.base, "aster-token")
        self.assertEqual(client.sticker_list("aster")["stickers"][0]["id"], "smile")
        downloaded = client.read_sticker("aster", "smile")
        self.assertEqual((downloaded["bytes"], downloaded["mime"]), (PNG, "image/png"))

    def test_auth_visibility_and_not_found(self):
        self.assertEqual(self.request("GET", "/v1/stickers?entity=aster", token="")[0], 401)
        self.assertEqual(self.request("GET", "/v1/stickers/aster/smile", token="")[0], 401)
        for token in ("aster-token", "guest-token"):
            self.assertEqual(self.request("GET", "/v1/stickers?entity=nori", token=token)[0], 404)
            self.assertEqual(self.request("GET", "/v1/stickers/nori/smile", token=token)[0], 404)
        status, _, raw = self.request("GET", "/v1/stickers", token="aster-token")
        self.assertEqual(json.loads(raw)["entity"], "aster")
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/v1/stickers?entity=nori", token="nori-token")[0], 200)
        self.assertEqual(self.request("GET", "/v1/stickers/aster/missing")[0], 404)
        self.assertEqual(self.request("GET", "/v1/stickers/aster/smile/extra")[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
