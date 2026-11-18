#!/usr/bin/env python3
"""公共區工具的閘（owner 2026-11-07「工具按照你说的做」）。

回帖／點讚走**現役** MCP `board`（不另寫第二套），閘加在 server 那側：

  ① `post`：每天 ≤3 條（發帖本身另按一次外網計價）；
  ① `reply`：同一條帖只回一次（跨行程一致）、每天 ≤5 條；
  ② `like`：每天 ≤10 次；
  ③ 滿了回機器可讀的碼（`already_replied`／`reply_daily_limit`／`like_daily_limit`）並以 429 拒絕。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent" / "tools"))


class CapsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-board-caps-")
        self.addCleanup(self.tmp.cleanup)
        os.environ["BOARD_DATA"] = self.tmp.name
        os.environ["BOARD_ACTOR"] = "from:entity"
        sys.modules.pop("mcp_board_server", None)
        import mcp_board_server

        self.server = mcp_board_server
        self.addCleanup(os.environ.pop, "BOARD_DATA", None)

    class Service:
        def __init__(self) -> None:
            self.calls = []

        def post(self, actor, text, media=(), client_mutation_id="", source_ref=""):
            self.calls.append(("post", actor.id, str(text)[:12]))
            return {"post": {"id": "pst_new"}}

        def reply(self, actor, post_id, text, media=(), client_mutation_id=""):
            self.calls.append(("reply", actor.id, post_id))
            return {"post": {"id": "pst_reply"}}

        def like(self, actor, post_id):
            self.calls.append(("like", actor.id, post_id))
            return {"added": True, "post": {"id": post_id}}

    def call(self, name, **args):
        service = self.Service()
        payload = dict(args, entity="aster")
        self.server._call(service, name, payload)
        return service.calls

    def test_post_daily_cap(self):
        for index in range(self.server.POST_PER_DAY):
            self.call("post", text=f"今日の記録 {index}")
        with self.assertRaises(self.server.BoardError) as caught:
            self.call("post", text="もう一本")
        self.assertEqual(caught.exception.code, "post_daily_limit")

    def test_post_counts_as_a_costed_action(self):
        """發帖在自主動作的帳上算一次外網（owner 2026-11-08「和其他活动一样，有消耗」）。"""
        from agent.brain.runtime.life_step import NET_TOOLS
        self.assertIn("mcp_board_post", NET_TOOLS)

    def test_reply_is_once_per_post(self):
        self.call("reply", id="pst_1", text="はい")
        with self.assertRaises(self.server.BoardError) as caught:
            self.call("reply", id="pst_1", text="もう一度")
        self.assertEqual(caught.exception.code, "already_replied")

    def test_reply_daily_cap(self):
        for index in range(self.server.REPLY_PER_DAY):
            self.call("reply", id=f"pst_{index}", text="はい")
        with self.assertRaises(self.server.BoardError) as caught:
            self.call("reply", id="pst_extra", text="まだ")
        self.assertEqual(caught.exception.code, "reply_daily_limit")

    def test_like_daily_cap(self):
        for index in range(self.server.LIKE_PER_DAY):
            self.call("like", id=f"pst_l{index}")
        with self.assertRaises(self.server.BoardError) as caught:
            self.call("like", id="pst_lx")
        self.assertEqual(caught.exception.code, "like_daily_limit")

    def _failing(self, code):
        server = self.server

        class Broken(self.Service):
            def post(self, *_a, **_k):
                raise server.BoardError(413, code)

            def reply(self, *_a, **_k):
                raise server.BoardError(413, code)
        return Broken()

    def test_failed_post_does_not_use_up_the_daily_quota(self):
        """REVIEW 2026-11-08 A5：App Server 没收下的帖不能算进每日上限。"""
        for _ in range(self.server.POST_PER_DAY + 2):
            with self.assertRaises(self.server.BoardError):
                self.server._call(self._failing("body_too_long"), "post",
                                  {"entity": "aster", "text": "長すぎ"})
        for index in range(self.server.POST_PER_DAY):    # 额度还是满的
            self.call("post", text=f"今日の記録 {index}")

    def test_failed_reply_can_be_retried_on_the_same_post(self):
        with self.assertRaises(self.server.BoardError) as first:
            self.server._call(self._failing("body_too_long"), "reply",
                              {"entity": "aster", "id": "pst_9", "text": "長すぎ"})
        self.assertEqual(first.exception.code, "body_too_long")
        self.assertEqual(self.call("reply", id="pst_9", text="短く"), [("reply", "aster", "pst_9")])

    def test_usage_is_shared_by_file_not_process_memory(self):
        self.call("like", id="pst_l1")
        stored = pathlib.Path(self.tmp.name, "board-access.json").read_text(encoding="utf-8")
        self.assertIn("like", stored, "用量要落盤，另一次 MCP 行程才看得到")


if __name__ == "__main__":
    unittest.main(verbosity=2)
