#!/usr/bin/env python3
"""網路傳輸瘦身（owner 2026-10-30「先做你說的 5 項」）。

判據：
  ① 送給 App 的語音降到 16 kHz；只動送出去的那一份（原本的 frame 不變，Discord／outbox 照用原檔）；
  ② `?w=` 縮圖：只收固定幾個寬；ETag＝原圖雜湊＋寬（端末拿清單的雜湊就推得出來）；
     ffmpeg 不在 ⇒ 給原圖、ETag 照縮圖那樣標；縮好的放快取、原檔換了就重做；
  ③ 縮圖照樣吃 304、照樣要登入、別人的相片照樣 404。
"""

from __future__ import annotations

import base64
import io
import json
import os
import pathlib
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.brain.media import app_audio, digest, thumbs
from agent.brain.media.store import MediaStore
from test_stickers import PNG, StickerTurnCase


def make_wav(rate: int, seconds: float = 0.5) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x10\x00\xf0\xff" * int(rate * seconds / 2))
    return out.getvalue()


def rate_of(wav: bytes) -> int:
    with wave.open(io.BytesIO(wav), "rb") as handle:
        return handle.getframerate()


@unittest.skipIf(app_audio.audioop is None, "這個 Python 沒有 audioop（3.13+）")
class TestSlimSpeech(unittest.TestCase):
    def test_wav_goes_down_to_16k_and_shrinks(self):
        source = make_wav(32000)
        slim = app_audio.slim_wav(source, 16000)
        self.assertEqual(rate_of(slim), 16000)
        self.assertLess(len(slim), len(source) * 0.6)

    def test_never_upsamples_and_leaves_non_wav_alone(self):
        low = make_wav(16000)
        self.assertIs(app_audio.slim_wav(low, 16000), low)
        self.assertEqual(app_audio.slim_wav(b"not a wav", 16000), b"not a wav")

    def test_speech_frame_is_copied_not_mutated(self):
        wav = make_wav(32000)
        frame = {"kind": "speech", "payload": {"audio": base64.b64encode(wav).decode(), "index": 0}}
        before = json.dumps(frame, sort_keys=True)
        sent = app_audio.slim_speech_event("speech", frame)
        self.assertEqual(json.dumps(frame, sort_keys=True), before, "原本的 frame 不能被改（outbox／Discord 還要用）")
        self.assertEqual(rate_of(base64.b64decode(sent["payload"]["audio"])), 16000)
        self.assertEqual(sent["payload"]["index"], 0)

    def test_other_events_and_switch_off(self):
        done = {"kind": "done", "payload": {"text": "x"}}
        self.assertIs(app_audio.slim_speech_event("done", done), done)
        wav = make_wav(32000)
        with mock.patch.dict(os.environ, {"BLUEAGENT_APP_AUDIO_RATE": "0"}):
            self.assertIs(app_audio.slim_wav(wav), wav)

    def test_sse_writer_sends_16k(self):
        """直接叫 HTTP 層寫 SSE 的那一支：寫出去的位元組裡是 16 kHz，原本的 frame 不變。"""
        from agent.server import http as app_server
        handler = app_server.Handler.__new__(app_server.Handler)
        handler.wfile = io.BytesIO()
        wav = make_wav(32000)
        frame = {"kind": "speech", "payload": {"audio": base64.b64encode(wav).decode(), "text": "x"}}
        handler._event("speech", frame)
        written = handler.wfile.getvalue().decode("utf-8")
        data = json.loads(written.split("data: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(rate_of(base64.b64decode(data["payload"]["audio"])), 16000)
        self.assertEqual(rate_of(base64.b64decode(frame["payload"]["audio"])), 32000)

    def test_http_layer_slims_every_sse_event(self):
        http = (pathlib.Path(__file__).resolve().parents[2] / "agent" / "server" / "http.py").read_text(encoding="utf-8")
        body = http[http.index("    def _event(self, event: str, payload: dict"):]
        # `_event` 之後接的是下一個方法（`_sse_name` 已抽到 `server/sse_names.py`）
        body = body[:body.index("\n    def ", 1)]
        self.assertIn("app_audio.slim_speech_event(event, payload, audio_profile)", body)


def fake_ffmpeg(folder: pathlib.Path) -> pathlib.Path:
    """假的 ffmpeg：把 `-i` 的檔原樣抄到最後一個參數（夠測快取與流程，不必真的縮）。"""
    script = folder / "ffmpeg"
    script.write_text('#!/bin/sh\nsrc=""\nprev=""\nfor a in "$@"; do\n'
                      '  [ "$prev" = "-i" ] && src="$a"\n  prev="$a"\n  last="$a"\ndone\n'
                      'cp "$src" "$last"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class TestThumbs(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-thumbs-"))
        self.source = self.root / "a.png"
        self.source.write_bytes(PNG)

    def test_only_known_widths(self):
        self.assertEqual(thumbs.pick_width(["192"][0]), 192)
        self.assertEqual(thumbs.pick_width("999"), 0)
        self.assertEqual(thumbs.pick_width("abc"), 0)
        self.assertIsNone(thumbs.thumbnail(self.source, 999, self.root / "cache"))

    def test_no_ffmpeg_means_none(self):
        with mock.patch.dict(os.environ, {"BLUEAGENT_FFMPEG": "definitely-not-here"}):
            self.assertIsNone(thumbs.thumbnail(self.source, 192, self.root / "cache"))

    def test_both_images_install_ffmpeg(self):
        """沒有 ffmpeg 就一律回原圖：App Server 的映像以前沒裝（PENDING B.App.5，2026-11-16 補上）。"""
        root = pathlib.Path(__file__).resolve().parents[2]
        for dockerfile in ("app/Dockerfile", "agent/Dockerfile"):
            with self.subTest(dockerfile):
                text = (root / dockerfile).read_text(encoding="utf-8")
                self.assertRegex(text, r"apt-get install[^\n]*(?:\\\n[^\n]*)*\bffmpeg\b")

    def test_cached_and_redone_when_the_source_changes(self):
        ffmpeg = fake_ffmpeg(self.root)
        with mock.patch.dict(os.environ, {"BLUEAGENT_FFMPEG": str(ffmpeg)}):
            first = thumbs.thumbnail(self.source, 192, self.root / "cache")
            self.assertIsNotNone(first)
            self.assertEqual(first.suffix, ".png", "PNG 要留透明")
            self.assertEqual(thumbs.thumbnail(self.source, 192, self.root / "cache"), first)
            self.source.write_bytes(PNG + b"\0")
            os.utime(self.source, ns=(1, 2))
            self.assertNotEqual(thumbs.thumbnail(self.source, 192, self.root / "cache"), first)

    def test_tag_is_derivable_from_the_original(self):
        self.assertEqual(thumbs.thumb_tag("abc", 192), "abc-w192")
        self.assertEqual(thumbs.thumb_tag("abc", 0), "abc")


class TestThumbHttp(StickerTurnCase):
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
        data = pathlib.Path(tempfile.mkdtemp(prefix="ba-data-"))
        env = mock.patch.dict(os.environ, {"BLUEAGENT_DATA_DIR": str(data),
                                           "BLUEAGENT_FFMPEG": str(fake_ffmpeg(data))})
        env.start()
        self.addCleanup(env.stop)

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

    def test_sticker_thumb_tag_follows_the_list_hash(self):
        _, _, body = self.get("/api/stickers?entity=mira", token=self.token)
        row = json.loads(body)["stickers"][0]
        status, etag, data = self.get(row["url"] + "?w=192", token=self.token)
        self.assertEqual((status, etag), (200, f'"{row["hash"]}-w192"'))
        self.assertTrue(data)
        self.assertEqual(self.get(row["url"] + "?w=192", token=self.token,
                                  tag=row["hash"] + "-w192")[::2], (304, b""))
        self.assertEqual(self.get(row["url"] + "?w=192")[0], 401)
        # 不認得的寬 ⇒ 原圖
        status, etag, _ = self.get(row["url"] + "?w=999", token=self.token)
        self.assertEqual(etag, f'"{row["hash"]}"')

    def test_media_thumb_and_privacy(self):
        self.state.media = MediaStore(pathlib.Path(tempfile.mkdtemp(prefix="ba-media-")))
        mine = self.state.media.save(PNG, ext=".png", principal="owner")
        self.platform.services.media.import_legacy(mine, PNG, "owner")
        status, etag, _ = self.get(f"/api/media/{mine['id']}?w=360", token=self.token)
        self.assertEqual((status, etag), (200, f'"{digest.content_tag(PNG)}-w360"'))
        other = self.state.media.save(PNG, ext=".png", principal="someone-else")
        self.assertEqual(self.get(f"/api/media/{other['id']}?w=360", token=self.token)[0], 404)

    def test_without_ffmpeg_the_original_is_served_with_the_thumb_tag(self):
        with mock.patch.dict(os.environ, {"BLUEAGENT_FFMPEG": "definitely-not-here"}):
            _, _, body = self.get("/api/stickers?entity=mira", token=self.token)
            row = json.loads(body)["stickers"][0]
            status, etag, data = self.get(row["url"] + "?w=192", token=self.token)
        self.assertEqual((status, etag, data), (200, f'"{row["hash"]}-w192"', PNG))


if __name__ == "__main__":
    unittest.main(verbosity=2)
