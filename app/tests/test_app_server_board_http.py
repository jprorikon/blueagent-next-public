#!/usr/bin/env python3
"""公共区的客户端 HTTP 面（PENDING B.App.8）：手机直连 App Server，不再经 Agent 的 `/api/feed`／`/api/post`。

判据：
  ① 投稿 → 读流 → 详情 → 回复 → 点赞 → 删除，整条链只碰 App Server；
  ② HTTP 与 MCP 回的是同一份东西（同一实现，不是第二份）；
  ③ 幂等键重放回同一篇；
  ④ 角色账号只读：写一律 403 `board_use_mcp`（每日上限在 Agent 的 MCP 闸，HTTP 不给绕过的路）；
  ⑤ 权限与错误码照旧：匿名 401、删别人的帖 403、删掉之后 404、不是这个形状的路径 404。
"""

from __future__ import annotations

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


class BoardHttpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-app-board-http-")
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        accounts = root / "accounts.json"
        rows = [
            ("owner", "owner", "主人", "owner-token", None),
            ("aster", "entity", "アスター", "aster-token", None),
            ("guest", "guest", "客人", "guest-token", ["aster"]),
        ]
        accounts.write_text(json.dumps({"schema_version": 1, "accounts": [
            {"id": identity, "kind": kind, "display": display, "visible_entities": visible,
             "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
            for identity, kind, display, token, visible in rows
        ]}), encoding="utf-8")
        self.server = make_server(AppConfig("127.0.0.1", 0, root / "data", accounts))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.thread.join, 2.0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, token="owner-token"):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else None

    def mcp(self, name, arguments=None, token="owner-token"):
        status, reply = self.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }, token=token)
        self.assertEqual(status, 200, reply)
        return json.loads(reply["result"]["content"][0]["text"])

    def ok(self, method, path, body=None, token="owner-token"):
        status, reply = self.request(method, path, body, token=token)
        self.assertEqual(status, 200, reply)
        return reply

    def test_full_cycle_touches_only_the_app_server(self):
        post = self.ok("POST", "/v1/posts", {"text": "今日の空", "client_mutation_id": "cm_one"})["post"]
        self.assertEqual((post["author_id"], post["body"]), ("owner", "今日の空"))

        feed = self.ok("GET", "/v1/feed?limit=20")
        self.assertEqual([item["id"] for item in feed["posts"]], [post["id"]])

        reply = self.ok("POST", f"/v1/posts/{post['id']}/replies",
                        {"text": "いいね", "client_mutation_id": "cm_reply"}, token="guest-token")
        self.assertEqual(reply["post"]["parent_id"], post["id"])

        liked = self.ok("POST", f"/v1/posts/{post['id']}/like", token="guest-token")
        again = self.ok("POST", f"/v1/posts/{post['id']}/like", token="guest-token")
        self.assertEqual((liked["added"], again["added"]), (True, False))

        detail = self.ok("GET", f"/v1/posts/{post['id']}", token="guest-token")
        self.assertEqual((detail["post"]["likes"], detail["post"]["liked"]), (1, True))
        self.assertEqual([item["id"] for item in detail["replies"]], [reply["post"]["id"]])

        self.assertEqual(self.ok("DELETE", f"/v1/posts/{post['id']}")["deleted"], True)
        status, gone = self.request("GET", f"/v1/posts/{post['id']}")
        self.assertEqual(status, 404, gone)

    def test_http_and_mcp_answer_with_the_same_payload(self):
        post = self.ok("POST", "/v1/posts", {"text": "同じ実装"})["post"]
        self.assertEqual(self.ok("GET", "/v1/feed?limit=5"), self.mcp("feed", {"limit": 5}))
        self.assertEqual(self.ok("GET", f"/v1/posts/{post['id']}"), self.mcp("get", {"id": post["id"]}))

    def test_mutation_id_replays_the_same_post(self):
        first = self.ok("POST", "/v1/posts", {"text": "一回だけ", "client_mutation_id": "cm_same"})
        second = self.ok("POST", "/v1/posts", {"text": "一回だけ", "client_mutation_id": "cm_same"})
        self.assertEqual(first["post"]["id"], second["post"]["id"])
        self.assertEqual(len(self.ok("GET", "/v1/feed")["posts"]), 1)

    def test_since_and_authors_filters_reach_the_service(self):
        mine = self.ok("POST", "/v1/posts", {"text": "主人の"})["post"]
        aster = self.mcp("post", {"text": "アスターの"}, token="aster-token")["post"]
        only_aster = self.ok("GET", "/v1/feed?authors=aster")
        self.assertEqual([item["id"] for item in only_aster["posts"]], [aster["id"]])
        later = self.ok("GET", f"/v1/feed?since={mine['at'] + 3600}")
        self.assertEqual(later["posts"], [])

    def test_entity_account_reads_but_writes_only_through_mcp(self):
        target = self.ok("POST", "/v1/posts", {"text": "読んでね"})["post"]
        self.assertTrue(self.ok("GET", "/v1/feed", token="aster-token")["posts"])
        for method, path, body in (("POST", "/v1/posts", {"text": "x"}),
                                   ("POST", f"/v1/posts/{target['id']}/replies", {"text": "x"}),
                                   ("POST", f"/v1/posts/{target['id']}/like", None),
                                   ("DELETE", f"/v1/posts/{target['id']}", None)):
            status, reply = self.request(method, path, body, token="aster-token")
            self.assertEqual((status, reply.get("error")), (403, "board_use_mcp"), (method, path))
        self.assertTrue(self.mcp("like", {"id": target["id"]}, token="aster-token")["added"])

    def test_permissions_and_shapes(self):
        target = self.ok("POST", "/v1/posts", {"text": "主人の帖"})["post"]
        self.assertEqual(self.request("GET", "/v1/feed", token="")[0], 401)
        status, reply = self.request("DELETE", f"/v1/posts/{target['id']}", token="guest-token")
        self.assertEqual(status, 403, reply)
        for method, path in (("GET", f"/v1/posts/{target['id']}/x"),
                             ("POST", f"/v1/posts/{target['id']}/a/b"),
                             ("DELETE", f"/v1/posts/{target['id']}/like"),
                             ("DELETE", "/v1/feed")):
            self.assertEqual(self.request(method, path, {} if method == "POST" else None)[0], 404,
                             (method, path))
        self.assertEqual(self.request("GET", "/v1/feed?limit=abc")[0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
