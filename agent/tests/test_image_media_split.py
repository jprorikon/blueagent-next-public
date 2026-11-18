#!/usr/bin/env python3
"""`/api/image` 的把關：端末拿到的 media 與**管線拿到的 media** 是兩份，不能互相蓋掉。

2026-10-27 的實況：owner 發圖，她回「写真、ですか。何を写したものを…」（等於沒看到圖）。
取證：`pipeline=media.see` 有跑，但 `prepare_image` 把 `data_out["media"]` 換成
「只給端末看的 4 個欄位」（id/ext/bytes/at），`server/http.py` 又把它 pop 出來當 SSE 首幀 ⇒
送進 `send_stream` 的 media 沒有 `path` ⇒ VisionStage 連 VL 都沒叫，直接寫「（読めませんでした）」。

判據（這一支就守這兩條）：
  ① `media`（給管線）要帶 `path`／`mime`，檔案真的存在；
  ② `media_public`（給端末的 SSE 首幀）只有 id/ext/bytes/at，不含 path。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

import agent.server.api as api
from agent.brain.media.store import MediaStore

# 1x1 PNG（最小的合法圖檔，不需要 Pillow）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000001e221bc330000000049454e44ae"
    "426082")


class _Owner:
    """準備階段只用到這幾個屬性（不比照真的 AuthContext，避免測試耦合認證實作）。"""

    principal = "owner"
    role = "owner"
    is_owner = True
    can_send = True

    def can(self, ability):
        return True


class TestImageMediaSplit(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        # 生產是 `server/main.py` 把媒體庫掛到 state 上（AppState 本身不建），測試照樣掛一份
        self.state.media = MediaStore(pathlib.Path(tempfile.mkdtemp(prefix="ba-media-")))

    def _prepare(self):
        status, payload = api.prepare_image(
            self.state, PNG,
            headers={"Content-Type": "image/png", "X-Entity": "mira", "X-Note": ""},
            auth=_Owner())
        self.assertEqual(status, 200, payload)
        return payload

    def test_pipeline_media_keeps_path(self):
        payload = self._prepare()
        media = payload["media"]
        self.assertTrue(media.get("path"), f"管線要拿到 path 才讀得到圖：{media}")
        self.assertTrue(pathlib.Path(media["path"]).exists(), media["path"])
        self.assertEqual(media.get("mime"), "image/png")
        self.assertTrue(payload["media"].get("id"))

    def test_public_media_has_no_path(self):
        payload = self._prepare()
        public = payload["media_public"]
        self.assertEqual(set(public), {"id", "ext", "bytes", "at"})
        self.assertNotIn("path", public)

    def test_http_layer_pops_only_public(self):
        """照 `server/http.py` 的順序做一次：pop 掉 public 之後，送進管線的那份還在。"""
        payload = self._prepare()
        public = dict(payload.pop("media_public", {}) or {})
        payload.pop("input", None)
        self.assertIn("id", public)
        self.assertTrue(payload["media"].get("path"))
        self.assertIn("media", payload)

    def test_want_voice_header_reaches_the_turn(self):
        """圖片那條也要能「聲あり」：端末帶 `X-Want-Voice: 1` ⇒ 這一輪帶上 want_voice。

        2026-10-27 實測：Unity 的 `/api/image` 從前沒帶這個 header ⇒ `speech=0`、
        `speech_errors=0`（服務端連合成都沒做），看起來就是「發圖之後她不講話」。
        """
        status, payload = api.prepare_image(
            self.state, PNG,
            headers={"Content-Type": "image/png", "X-Entity": "nori",
                     "X-Note": "", "X-Want-Voice": "1"},
            auth=_Owner())
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("want_voice"))

        status, silent = api.prepare_image(
            self.state, PNG,
            headers={"Content-Type": "image/png", "X-Entity": "nori", "X-Note": ""},
            auth=_Owner())
        self.assertEqual(status, 200, silent)
        self.assertFalse(silent.get("want_voice"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
