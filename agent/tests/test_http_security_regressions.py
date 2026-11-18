#!/usr/bin/env python3
"""公網 HTTP 入口的前置安全邊界（2026-11-04）。"""

from __future__ import annotations

import pathlib
import sys
import types
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))   # 倉庫根

from agent.server import http
from agent.server.api.media import store_max_bytes


class TestBinaryBodyGuards(unittest.TestCase):
    @staticmethod
    def handler(length: int):
        handler = object.__new__(http.Handler)
        handler.headers = {"Content-Length": str(length)}
        handler.rfile = mock.Mock()
        handler._json = mock.Mock()
        return handler

    def test_image_rejects_anonymous_before_reading(self):
        handler = self.handler(100)
        handler._image(auth=None, ip="", source="", external_id="")
        handler._json.assert_called_once_with(401, {"error": "unauthorized"})
        handler.rfile.read.assert_not_called()

    def test_audio_rejects_anonymous_before_reading(self):
        handler = self.handler(100)
        handler._audio(auth=None, ip="", source="", external_id="")
        handler._json.assert_called_once_with(401, {"error": "unauthorized"})
        handler.rfile.read.assert_not_called()

    def test_image_rejects_oversize_before_reading(self):
        limit = store_max_bytes()
        handler = self.handler(limit + 1)
        auth = types.SimpleNamespace(principal="owner")
        handler._image(auth=auth, ip="", source="", external_id="")
        handler._json.assert_called_once_with(
            413, {"error": "image_too_large", "limit": limit})
        handler.rfile.read.assert_not_called()

    def test_audio_rejects_oversize_before_reading(self):
        handler = self.handler(12_000_001)
        auth = types.SimpleNamespace(principal="owner")
        handler._audio(auth=auth, ip="", source="", external_id="")
        handler._json.assert_called_once_with(413, {"error": "audio too large"})
        handler.rfile.read.assert_not_called()


class TestTrpgKickGuards(unittest.TestCase):
    @staticmethod
    def handler(token: str, supplied: str = ""):
        handler = object.__new__(http.Handler)
        seat = types.SimpleNamespace(
            config=types.SimpleNamespace(kick_token=token), handle_kick=mock.Mock())
        handler._state = lambda: types.SimpleNamespace(app=types.SimpleNamespace(trpg=seat))
        handler.headers = {"X-Table-Kick-Token": supplied} if supplied else {}
        handler._json = mock.Mock()
        return handler, seat

    def test_missing_configured_token_fails_closed(self):
        handler, seat = self.handler("")
        with mock.patch("threading.Thread") as thread:
            handler._trpg_kick({})
        handler._json.assert_called_once_with(
            503, {"ok": False, "error": "kick_token_not_configured"})
        thread.assert_not_called()
        seat.handle_kick.assert_not_called()

    def test_wrong_token_is_rejected_and_valid_token_is_accepted(self):
        handler, seat = self.handler("secret", "wrong")
        with mock.patch("threading.Thread") as thread:
            handler._trpg_kick({})
        handler._json.assert_called_once_with(403, {"ok": False, "error": "bad token"})
        thread.assert_not_called()
        seat.handle_kick.assert_not_called()

        handler, _seat = self.handler("secret", "secret")
        with mock.patch("threading.Thread") as thread:
            handler._trpg_kick({"table_id": "table-a"})
        handler._json.assert_called_once_with(200, {"ok": True, "taken": True})
        thread.assert_called_once()

    def test_seat_without_kick_token_is_not_ready(self):
        from agent.adapter.trpg import TrpgConfig
        self.assertFalse(TrpgConfig(enabled=True, seat="p1").ready)
        self.assertTrue(TrpgConfig(enabled=True, seat="p1", kick_token="secret").ready)


if __name__ == "__main__":
    unittest.main(verbosity=2)
