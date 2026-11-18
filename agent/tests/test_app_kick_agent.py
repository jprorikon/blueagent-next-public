#!/usr/bin/env python3
"""`POST /api/app/kick` 的 Agent 側：名單分發、未知名單、token 閘、wake 冪等。

對應規格 `docs/PLAN-app-kick-20261108.md` §5／§6。
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.server.api.app_kick import handle, kick, token


class _Adapter:
    """只用到 `entity` 與 `wake()`：kick 不該碰別的東西。"""

    def __init__(self, entity: str) -> None:
        self.entity = entity
        self.woken = 0
        self._event = threading.Event()

    def wake(self) -> None:
        self.woken += 1
        self._event.set()


class KickDispatchCase(unittest.TestCase):
    def setUp(self):
        self.mira, self.nori = _Adapter("mira"), _Adapter("nori")
        self.state = SimpleNamespace(app_chat=[self.mira, self.nori])

    def test_wakes_only_the_named_entities(self):
        result = kick(self.state, ["nori"])
        self.assertEqual(result, {"ok": True, "woken": ["nori"], "unknown": []})
        self.assertEqual((self.nori.woken, self.mira.woken), (1, 0))

    def test_unknown_entity_is_reported_not_fatal(self):
        result = kick(self.state, ["aster", "mira"])
        self.assertEqual(result["woken"], ["mira"])
        self.assertEqual(result["unknown"], ["aster"])

    def test_duplicates_and_blanks_collapse(self):
        kick(self.state, ["mira", "mira", "", None])
        self.assertEqual(self.mira.woken, 1)

    def test_empty_or_missing_list_is_noop(self):
        self.assertEqual(kick(self.state, []), {"ok": True, "woken": [], "unknown": []})
        self.assertEqual(kick(self.state, None), {"ok": True, "woken": [], "unknown": []})
        self.assertEqual(kick(SimpleNamespace(app_chat=None), ["mira"])["unknown"], ["mira"])

    def test_wake_is_a_hint_not_a_queue(self):
        """連按十次 kick ⇒ 只該結束一次等待（真正推進游標的是拉取）。"""
        adapter = _Adapter("mira")
        for _ in range(10):
            kick(SimpleNamespace(app_chat=[adapter]), ["mira"])
        self.assertEqual(adapter.woken, 10, "kick 每次都該置位（重複無害）")
        started = time.monotonic()
        adapter._event.wait(1.0)
        self.assertLess(time.monotonic() - started, 0.5, "一次 wake 就該讓等待立刻結束")


class TokenGateCase(unittest.TestCase):
    def test_token_comes_from_env_and_blank_means_disabled(self):
        with mock.patch.dict(os.environ, {"BLUEAGENT_APP_KICK_TOKEN": "  "}, clear=False):
            self.assertEqual(token(), "")
        with mock.patch.dict(os.environ, {"BLUEAGENT_APP_KICK_TOKEN": "abc"}, clear=False):
            self.assertEqual(token(), "abc")

    def test_handle_gates_on_the_header_token(self):
        """`/api/app/kick` 整支：沒設 token ⇒ 503；對不上 ⇒ 403、誰都不叫；對上 ⇒ 200 照名單叫。"""
        adapter = _Adapter("nori")
        state = SimpleNamespace(app_chat=[adapter])
        body = {"entities": ["nori"]}
        with mock.patch.dict(os.environ, {"BLUEAGENT_APP_KICK_TOKEN": ""}, clear=False):
            self.assertEqual(handle(state, "abc", body)[0], 503)
        with mock.patch.dict(os.environ, {"BLUEAGENT_APP_KICK_TOKEN": "abc"}, clear=False):
            self.assertEqual(handle(state, "nope", body)[0], 403)
            self.assertEqual(handle(state, None, body)[0], 403)
            self.assertEqual(adapter.woken, 0)
            status, reply = handle(state, "abc", body)
        self.assertEqual((status, reply["woken"]), (200, ["nori"]))


if __name__ == "__main__":
    unittest.main()
