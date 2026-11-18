#!/usr/bin/env python3
"""第二輪網路瘦身：精簡 view、parts-v1、gzip、μ-law 與音檔 Range。"""

from __future__ import annotations

import base64
import gzip
import io
import json
import pathlib
import struct
import sys
import unittest
import wave

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))   # 倉庫根

from agent.brain.media import app_audio  # noqa: E402
from agent.server import http as app_http  # noqa: E402
from agent.server.api import system  # noqa: E402
from agent.server.http_encoding import accepts_gzip, byte_range  # noqa: E402
from agent.server.stream_profile import slim_done_event  # noqa: E402


def make_wav(rate: int = 16000, frames: int = 1600) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x10\x00\xf0\xff" * (frames // 2))
    return out.getvalue()


class Owner:
    principal = "owner"
    is_owner = True


class Affect:
    def mood(self, entity):
        return {"key": "happy", "revision": 8}

    def timeline(self, *args, **kwargs):
        raise AssertionError("badge 不應取 timeline")

    def stats(self, *args, **kwargs):
        raise AssertionError("badge 不應取 stats")


class MoodState:
    class App:
        affect = Affect()
    app = App()


class ResumeState:
    class App:
        affect = None
    app = App()

    def snapshot(self, **kwargs):
        return {"threads": [
            {"id": "a", "entity": "mira", "last_active_at": 4, "text": "不該傳"},
            {"id": "b", "entity": "aster", "last_active_at": 9, "text": "別人"},
        ], "memory": {"facts": ["不該傳"]}}


class TestSlimViews(unittest.TestCase):
    def test_badge_only_returns_the_snapshot(self):
        status, body = system.get_mood(
            MoodState(), {"entity": ["mira"], "view": ["badge"]}, auth=Owner())
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ok", "entity", "mood"})
        self.assertEqual(body["mood"]["revision"], 8)

    def test_resume_only_returns_current_entity_thread_cursor(self):
        status, body = system.get_state(
            ResumeState(), {"entity": ["mira"], "view": ["resume"]}, auth=Owner())
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "entity": "mira", "threads": [
            {"id": "a", "entity": "mira", "last_active_at": 4}]})


class TestPartsProfile(unittest.TestCase):
    def test_legacy_done_is_unchanged(self):
        frame = {"text": "全文", "bubbles": ["全文"], "turn_id": "t1"}
        self.assertIs(slim_done_event("done", frame), frame)

    def test_parts_done_drops_duplicate_text_and_keeps_hash(self):
        frame = {"payload": {"text": "全文", "bubbles": ["全文"], "turn_id": "t1"}}
        sent = slim_done_event("done", frame, "parts-v1")
        self.assertNotIn("text", sent["payload"])
        self.assertNotIn("bubbles", sent["payload"])
        self.assertEqual(sent["payload"]["turn_id"], "t1")
        self.assertTrue(sent["payload"]["text_hash"])
        self.assertEqual(frame["payload"]["text"], "全文", "權威 frame 不可被改")

    def test_error_or_missing_parts_text_is_not_dropped(self):
        error = {"text": "失敗原因", "error": "model_failed"}
        self.assertIs(slim_done_event("done", error, "parts-v1"), error)
        fallback = {"text": "分段器失敗時的唯一全文"}
        self.assertIs(slim_done_event("done", fallback, "parts-v1"), fallback)


class TestEncodingNegotiation(unittest.TestCase):
    def test_gzip_quality_and_ranges(self):
        self.assertTrue(accepts_gzip("br, gzip"))
        self.assertFalse(accepts_gzip("gzip;q=0, br"))
        self.assertTrue(accepts_gzip("*"))
        self.assertEqual(byte_range("bytes=2-5", 10), (2, 5))
        self.assertEqual(byte_range("bytes=7-", 10), (7, 9))
        self.assertEqual(byte_range("bytes=-3", 10), (7, 9))
        self.assertFalse(byte_range("bytes=10-12", 10))
        self.assertFalse(byte_range("bytes=0-1,4-5", 10))


class HandlerProbe:
    def __init__(self, headers=None):
        self.handler = app_http.Handler.__new__(app_http.Handler)
        self.handler.headers = headers or {}
        self.handler.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}
        self.handler.send_response = self._status
        self.handler.send_header = self.response_headers.__setitem__
        self.handler.end_headers = lambda: None

    def _status(self, status):
        self.status = status


class TestHttpRepresentations(unittest.TestCase):
    def test_large_json_is_gzipped_and_etag_is_weak(self):
        probe = HandlerProbe({"Accept-Encoding": "gzip"})
        payload = {"rows": ["中文" * 400]}
        probe.handler._json(200, payload, etag="abc")
        self.assertEqual(probe.status, 200)
        self.assertEqual(probe.response_headers["Content-Encoding"], "gzip")
        self.assertEqual(probe.response_headers["Vary"], "Accept-Encoding")
        self.assertEqual(probe.response_headers["ETag"], 'W/"abc"')
        self.assertEqual(json.loads(gzip.decompress(probe.handler.wfile.getvalue())), payload)

    def test_small_json_and_unaccepted_gzip_stay_plain(self):
        small = HandlerProbe({"Accept-Encoding": "gzip"})
        small.handler._json(200, {"ok": True})
        self.assertNotIn("Content-Encoding", small.response_headers)
        plain = HandlerProbe({"Accept-Encoding": "gzip;q=0"})
        plain.handler._json(200, {"rows": ["x" * 3000]})
        self.assertNotIn("Content-Encoding", plain.response_headers)

    def test_audio_range_and_etag(self):
        audio = make_wav(frames=20)
        partial = HandlerProbe({"Range": "bytes=10-19"})
        partial.handler._turn_audio(audio)
        self.assertEqual(partial.status, 206)
        self.assertEqual(partial.handler.wfile.getvalue(), audio[10:20])
        self.assertEqual(partial.response_headers["Content-Range"],
                         f"bytes 10-19/{len(audio)}")
        tag = partial.response_headers["ETag"].strip('"')
        cached = HandlerProbe({"If-None-Match": f'W/"{tag}"'})
        cached.handler._turn_audio(audio)
        self.assertEqual(cached.status, 304)
        self.assertEqual(cached.handler.wfile.getvalue(), b"")


@unittest.skipIf(app_audio.audioop is None, "這個 Python 沒有 audioop")
class TestMuLawTransport(unittest.TestCase):
    def test_mulaw_wav_halves_pcm_payload_and_decodes(self):
        source = make_wav()
        packed = app_audio.transport_wav(source, "mulaw-v1")
        self.assertEqual(struct.unpack_from("<H", packed, 20)[0], 7)
        self.assertEqual(struct.unpack_from("<H", packed, 34)[0], 8)
        self.assertLess(len(packed), len(source) * 0.55)
        data_at = packed.index(b"data") + 8
        decoded = app_audio.audioop.ulaw2lin(packed[data_at:data_at + 1600], 2)
        with wave.open(io.BytesIO(source), "rb") as original:
            self.assertEqual(len(decoded), len(original.readframes(original.getnframes())))

    def test_profile_is_opt_in_and_frame_is_not_mutated(self):
        source = make_wav()
        encoded = base64.b64encode(source).decode()
        frame = {"payload": {"audio": encoded}}
        legacy = app_audio.slim_speech_event("speech", frame)
        self.assertIs(legacy, frame, "已經是 16 kHz 的舊客戶端不必重寫")
        sent = app_audio.slim_speech_event("speech", frame, "mulaw-v1")
        self.assertEqual(sent["payload"]["audio_codec"], "mulaw")
        self.assertEqual(frame["payload"]["audio"], encoded)
        self.assertLess(len(sent["payload"]["audio"]), len(encoded) * 0.55)


if __name__ == "__main__":
    unittest.main(verbosity=2)
