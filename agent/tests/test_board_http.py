#!/usr/bin/env python3
"""公共区与 Agent 的边界（2026-11-08 起）：

  ① Agent 不再提供 `/api/feed`、`/api/post…`——客户端直连 App Server `/v1/feed`、`/v1/posts`；
  ② Agent 的生活记录不会自动变成帖（只有她显式经 MCP 发的才算）；
  ③ 旧消息里的 `/api/media/<id>` 仍可读，授权跟着 App Server 上的公开帖走（帖删了就收回）；
  ④ 投稿／回复只收调用者自己上传的新媒体。
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from demo_common import make_state, owner_headers
from platform_server import start_platform

from agent.server.http import Handler, set_state


def _call(url, method="GET", body=None, token="", content_type="application/json", raw=False):
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = response.read()
            return response.status, payload if raw else json.loads(payload or b"null")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, payload if raw else json.loads(payload or b"null")


class TestBoardBoundary(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.state.app.shutdown_lives()
        self.state.media = self.state.app.media
        self.owner_token = self.state.app.auth.owner_token
        self.platform = start_platform(self, [
            ("owner", "owner", self.owner_token),
            ("guest-a", "guest", "guest-a-token"),
        ])
        self.app = self.platform.test_base_url
        set_state(self.state)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.agent = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.state.shutdown)
        self.addCleanup(self.thread.join, 2.0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def agent_call(self, method, path, body=None):
        request = urllib.request.Request(
            self.agent + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={**owner_headers(self.state), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def app_call(self, method, path, body=None, token=None):
        return _call(self.app + path, method, body, token or self.owner_token)

    def test_agent_no_longer_serves_the_board(self):
        created = self.app_call("POST", "/v1/posts", {"text": "App Server だけ"})[1]["post"]
        for method, path, body in (("GET", "/api/feed", None),
                                   ("GET", "/api/post/" + created["id"], None),
                                   ("POST", "/api/post", {"text": "x"}),
                                   ("POST", f"/api/post/{created['id']}/reply", {"text": "x"}),
                                   ("POST", f"/api/post/{created['id']}/like", {}),
                                   ("DELETE", "/api/post/" + created["id"], None)):
            self.assertEqual(self.agent_call(method, path, body), 404, (method, path))
        status, feed = self.app_call("GET", "/v1/feed")
        self.assertEqual(status, 200, feed)
        self.assertEqual([post["id"] for post in feed["posts"]], [created["id"]])

    def test_agent_activity_is_not_automatically_projected(self):
        for entity in ("aster", "nori"):
            self.state.app.activity_log.append(
                entity, kind="study", text=f"{entity} 的公开收尾", visibility="public",
                activity="公开事", step=0, activity_id=f"activity-{entity}")
        status, payload = self.app_call("GET", "/v1/feed")
        self.assertEqual(status, 200, payload)
        self.assertFalse([post for post in payload["posts"] if post["origin"] == "projection"])

    def test_legacy_media_link_follows_the_public_post(self):
        mine = self.state.media.save(b"owner image", ext=".png", principal="owner")
        other = self.state.media.save(b"guest image", ext=".png", principal="guest-a")
        _, guest_token = self.state.app.accounts.issue(
            role="guest", principal="guest-a", token="guest-a-token")
        for row, who in ((mine, "owner"), (other, "guest-a")):
            self.platform.services.media.import_legacy(
                row, self.state.media.path_of(row["id"]).read_bytes(), who)

        def legacy(media_id, bearer):
            return _call(self.agent + "/api/media/" + media_id, token=bearer, raw=True)

        self.assertEqual(legacy(mine["id"], guest_token)[0], 404)
        status, denied = self.app_call("POST", "/v1/posts", {"text": "盗图", "media": [other["id"]]})
        self.assertEqual((status, denied["error"]), (400, "unknown_media"))
        status, created = self.app_call("POST", "/v1/posts", {"text": "公开图", "media": [mine["id"]]})
        self.assertEqual(status, 200, created)
        self.assertEqual(legacy(mine["id"], guest_token), (200, b"owner image"))
        self.app_call("DELETE", "/v1/posts/" + created["post"]["id"])
        self.assertEqual(legacy(mine["id"], guest_token)[0], 404)
        self.assertEqual(legacy(mine["id"], self.owner_token), (200, b"owner image"))

    def test_post_and_reply_accept_only_callers_new_app_media(self):
        def upload(token, data):
            status, item = _call(self.app + "/v1/media", "POST", data, token, "image/png")
            self.assertEqual(status, 200, item)
            return item

        mine = upload(self.owner_token, b"new owner image")
        other = upload("guest-a-token", b"new other image")
        status, denied = self.app_call("POST", "/v1/posts", {"text": "他人的新媒体", "media": [other["id"]]})
        self.assertEqual((status, denied["error"]), (400, "unknown_media"))
        status, created = self.app_call("POST", "/v1/posts", {"text": "自己的新媒体", "media": [mine["id"]]})
        self.assertEqual(status, 200, created)
        self.assertEqual(created["post"]["author_id"], "owner")
        reply_image = upload(self.owner_token, b"new reply image")
        status, replied = self.app_call("POST", "/v1/posts/" + created["post"]["id"] + "/replies",
                                        {"text": "自己的回复图片", "media": [reply_image["id"]]})
        self.assertEqual(status, 200, replied)
        self.assertEqual(replied["post"]["media"], [reply_image["id"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
