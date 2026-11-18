#!/usr/bin/env python3
"""C-02／C-03 切换：Agent 侧（RemoteBoardService／stdio MCP）只写 App Server，凭证按调用者走。

2026-11-08 起 Agent 不再提供 `/api/feed`／`/api/post`，客户端直连 App Server；这里测的是 Agent 自己
仍在用的那一层：`state.board_for(auth)` 与 stdio MCP。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import unittest
import unittest.mock

from demo_common import make_state

from app.server.config import AppConfig
from app.server.http import make_server
from agent.brain.domain.auth import AuthContext, Role
from agent.adapter.app_protocol import Actor, BoardError
from agent.server.api.board import can_read_media

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("mcp_board_server_public", ROOT / "agent" / "tools" / "mcp_board_server.py")
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class AppPublicSwitchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-public-switch-")
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.state = make_state()
        self.state.app.shutdown_lives()
        self.addCleanup(self.state.shutdown)
        self.owner_token = self.state.app.auth.owner_token
        self.guest_token = "other-token"
        rows = [
            ("acct-owner", "owner", "主人", "owner", self.owner_token),
            ("acct-other", "guest", "别人", "guest-other", self.guest_token),
            ("aster", "entity", "アスター", "", "aster-token"),
        ]
        accounts = root / "accounts.json"
        accounts.write_text(json.dumps({"accounts": [
            {"id": identity, "kind": kind, "display": display, "legacy_principal": legacy,
             "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
            for identity, kind, display, legacy, token in rows]}), encoding="utf-8")
        self.app_server = make_server(AppConfig("127.0.0.1", 0, root / "app-data", accounts))
        self.thread = threading.Thread(target=self.app_server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.app_server.server_port}"
        self.addCleanup(self.thread.join, 2.0)
        self.addCleanup(self.app_server.server_close)
        self.addCleanup(self.app_server.shutdown)
        self.env = unittest.mock.patch.dict(os.environ, {
            "BLUEAGENT_APP_SERVER_URL": self.base,
            "BLUEAGENT_APP_SERVER_TOKENS": json.dumps({"aster": "aster-token"}),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.owner = AuthContext("owner", Role.OWNER, credential=self.owner_token)
        self.other = AuthContext("guest-other", credential=self.guest_token)

    def board(self, auth, method, *args, **kwargs):
        """(status, payload)：身份只看 auth 里的凭证，Actor 参数故意填假的，证明不会被拿去冒名。"""
        try:
            service = self.state.board_for(auth)
            return 200, getattr(service, method)(Actor("spoofed", "owner", "spoofed"), *args, **kwargs)
        except BoardError as exc:
            return exc.status, exc.payload()

    def target_posts(self):
        return self.app_server.services.board.store.manual_posts()

    def test_agent_side_operations_use_remote_truth_and_legacy_author_filter(self):
        old_db = self.state.app.data_dir / "board" / "board.db"
        self.assertFalse(old_db.exists())
        status, created = self.board(self.owner, "post", "旧入口远程投稿",
                                     client_mutation_id="cm-remote")
        self.assertEqual(status, 200, created)
        post_id = created["post"]["id"]
        self.assertEqual(created["post"]["author_id"], "acct-owner")
        self.assertEqual(len(self.target_posts()), 1)
        self.assertFalse(old_db.exists())

        status, filtered = self.board(self.owner, "feed", authors={"owner"})
        self.assertEqual(status, 200, filtered)
        self.assertIn(post_id, {row["id"] for row in filtered["posts"]})
        status, replied = self.board(self.other, "reply", post_id, "远程回复")
        self.assertEqual(status, 200, replied)
        self.assertEqual(replied["post"]["author_id"], "acct-other")
        status, liked = self.board(self.other, "like", post_id)
        self.assertEqual((status, liked["post"]["likes"]), (200, 1))
        status, detail = self.board(self.owner, "get", post_id)
        self.assertEqual((status, detail["post"]["replies"]), (200, 1))
        status, deleted = self.board(self.owner, "delete", post_id)
        self.assertEqual((status, deleted["deleted"]), (200, True))
        status, missing = self.board(self.owner, "get", post_id)
        self.assertEqual((status, missing["error"]), (404, "not_found"))

    def test_caller_credentials_do_not_leak_and_old_media_acl_uses_remote_snapshot(self):
        legacy = self.state.app.media.save(b"public image", ext=".png", principal="owner")
        data = self.state.app.media.path_of(legacy["id"]).read_bytes()
        self.app_server.services.media.import_legacy(legacy, data, "acct-owner")
        status, created = self.board(self.owner, "post", "远程公开图", [legacy["id"]])
        self.assertEqual(status, 200, created)
        self.assertTrue(can_read_media(self.state, legacy["id"], "owner", auth=self.other))
        self.assertTrue(can_read_media(self.state, legacy["id"], "owner", auth=self.owner))
        status, deleted = self.board(self.owner, "delete", created["post"]["id"])
        self.assertEqual(status, 200)
        self.assertFalse(can_read_media(self.state, legacy["id"], "owner", auth=self.other))
        self.assertTrue(can_read_media(self.state, legacy["id"], "owner", auth=self.owner))

        status, own = self.board(self.other, "post", "别人自己的帖子")
        self.assertEqual((status, own["post"]["author_id"]), (200, "acct-other"))
        status, denied = self.board(self.owner, "delete", own["post"]["id"])
        self.assertEqual(status, 200, denied)

    def test_stdio_uses_environment_credential_and_source_ref_idempotency(self):
        for name in ("POST_PER_DAY", "REPLY_PER_DAY", "LIKE_PER_DAY"):
            old = getattr(SERVER, name)
            setattr(SERVER, name, 0)
            self.addCleanup(setattr, SERVER, name, old)
        old_actor = SERVER.BOARD_ACTOR
        self.addCleanup(lambda: setattr(SERVER, "BOARD_ACTOR", old_actor))
        SERVER.BOARD_ACTOR = "aster"
        service = SERVER._service()
        before = len(self.target_posts())

        def call(text, source_ref):
            reply = SERVER.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "post", "arguments": {
                                       "text": text, "source_ref": source_ref,
                                       "actor": "owner", "entity": "owner"}}}, service)
            return reply["result"]

        first = call("角色显式发布", "activity:aster:explicit")
        replay = call("角色显式发布", "activity:aster:explicit")
        self.assertFalse(first.get("isError"), first)
        self.assertFalse(replay.get("isError"), replay)
        first_payload = json.loads(first["content"][0]["text"])
        replay_payload = json.loads(replay["content"][0]["text"])
        self.assertEqual(first_payload["post"]["author_id"], "aster")
        self.assertEqual(replay_payload["post"]["id"], first_payload["post"]["id"])
        self.assertTrue(replay_payload["idempotent_replay"])
        self.assertEqual(len(self.target_posts()), before + 1)

    def test_remote_failure_never_falls_back_to_local_writes(self):
        os.environ["BLUEAGENT_APP_SERVER_URL"] = "http://127.0.0.1:1"
        old_db = self.state.app.data_dir / "board" / "board.db"
        status, failed = self.board(self.owner, "post", "不能写进旧库")
        self.assertEqual((status, failed["error"]), (503, "app_server_unavailable"))
        self.assertFalse(old_db.exists())

        # 旧开关即使被显式关闭，也不能重新启用本地真值。
        os.environ["BLUEAGENT_APP_PUBLIC"] = "0"
        status, still_remote = self.board(self.owner, "post", "仍不能写进旧库")
        self.assertEqual((status, still_remote["error"]), (503, "app_server_unavailable"))
        self.assertFalse(old_db.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
