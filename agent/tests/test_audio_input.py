#!/usr/bin/env python3
"""語音輸入（STT）：音檔 → 文字 → **同一條管道**（權限/模式不因語音而放寬）。

判據：
  ① `/api/audio` 回 SSE，第一幀是 `transcript`（文字/語言/信度），後面才是正常回合；
  ② 轉出來的文字走**同一道** `prepare_send`：owner 仍是默認 work、guest 仍是 chat；
  ③ 裸 PCM16 也能用（自動包成 wav）；
  ④ 轉不出來 ⇒ 422 + 原樣回 transcript，**不編一句話**；沒開 STT ⇒ 503；
  ⑤ 音檔不進記憶：記憶裡只有那句話。
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403

from agent.adapter.stt_local import Transcript
from agent.brain.domain.auth import AuthContext, Role
from agent.server import api as api_module

OWNER = AuthContext(principal="owner", role=Role.OWNER, capabilities=frozenset({"chat"}))
GUEST = AuthContext(principal="guest-x", role=Role.GUEST, capabilities=frozenset({"chat"}))


class FakeSTT:
    """可控的轉寫器：要它回什麼就回什麼，順便記下收到的位元組。"""

    def __init__(self, text="おやすみ、オーナー。", language="ja", probability=0.95, error=""):
        self.text = text
        self.language = language
        self.probability = probability
        self.error = error
        self.calls: list[dict] = []

    def transcribe(self, audio, *, language="", suffix=".wav"):
        self.calls.append({"bytes": len(audio), "language": language, "suffix": suffix})
        return Transcript(text=self.text if not self.error else "", language=self.language,
                          probability=self.probability, seconds=1.5, engine="fake",
                          error=self.error)

    def pcm16_to_wav(self, pcm, *, rate=16000, channels=1):
        return b"RIFF" + bytes(len(pcm))

    def health(self):
        return {"ok": True, "model": "fake"}


def wav_bytes(payload=b"\x00\x00" * 16000) -> bytes:
    return b"RIFF" + payload


class AudioCase(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.stt = FakeSTT()
        self.state.app.stt = self.stt

    def prepare(self, audio, *, auth=OWNER, **headers):
        return api_module.prepare_audio(self.state, audio, headers=headers, auth=auth)


class TestPrepareAudio(AudioCase):
    def test_transcript_becomes_the_turn_text(self):
        status, data = self.prepare(wav_bytes())
        self.assertEqual(status, 200)
        self.assertEqual(data["text"], "おやすみ、オーナー。")
        self.assertEqual(data["input"], "audio")
        self.assertEqual(data["transcript"]["language"], "ja")

    def test_owner_still_defaults_to_work(self):
        _, data = self.prepare(wav_bytes())
        self.assertEqual(data["mode"], "work")

    def test_guest_stays_chat_even_by_voice(self):
        _, data = self.prepare(wav_bytes(), auth=GUEST, **{"X-Mode": "work"})
        self.assertEqual(data["mode"], "chat")
        self.assertEqual(data["mode_note"], "work_requires_owner")

    def test_raw_pcm_is_wrapped(self):
        status, _ = self.prepare(b"\x01\x02" * 8000)
        self.assertEqual(status, 200)
        self.assertEqual(self.stt.calls[0]["suffix"], ".wav")

    def test_empty_audio_is_rejected(self):
        status, data = self.prepare(b"")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "audio required")

    def test_stt_failure_is_422_and_no_text(self):
        self.stt.error = "ModelNotReady"
        status, data = self.prepare(wav_bytes())
        self.assertEqual(status, 422)
        self.assertEqual(data["error"], "stt_failed")
        self.assertEqual(data["transcript"]["text"], "")

    def test_missing_stt_is_503(self):
        self.state.app.stt = None
        status, data = self.prepare(wav_bytes())
        self.assertEqual(status, 503)

    def test_language_hint_reaches_stt(self):
        self.prepare(wav_bytes(), **{"X-Lang": "ja"})
        self.assertEqual(self.stt.calls[0]["language"], "ja")


class TestHttpAudio(AudioCase):
    def setUp(self):
        super().setUp()
        from agent.server import http as app_server
        self.app = app_server
        app_server.set_state(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def post_audio(self, audio, token=None, headers=None):
        request = urllib.request.Request(
            self.base + "/api/audio", data=audio, method="POST",
            headers={"Content-Type": "audio/wav",
                     "Authorization": f"Bearer {token or self.state.app.auth.owner_token}",
                     **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_sse_starts_with_transcript_then_turn(self):
        status, raw = self.post_audio(wav_bytes())
        self.assertEqual(status, 200)
        events = [block.split("\n")[0] for block in raw.split("\n\n") if block.strip()]
        self.assertTrue(events[0].startswith("event: transcript"), events[0])
        # 之後的幀事件名＝內層 kind（契約：App 直接 switch 事件名）
        self.assertEqual(json.loads(raw.split("\n\n")[0].split("data: ")[1])["text"],
                         "おやすみ、オーナー。")
        self.assertIn("event: result", raw)

    def test_want_voice_header_survives_the_http_layer(self):
        """`X-Want-Voice` 必須從 HTTP 層傳進 `prepare_audio`（Unity 語音簡訊靠它）。"""
        captured = {}

        def fake_stream(**kwargs):
            captured.update(kwargs)
            return iter(())

        def fake_voice(**kwargs):
            captured.update(kwargs)
            return {"frames": [], "following_up": False, "decision": {}}

        for name, fake in (("send_stream", fake_stream), ("send_voice", fake_voice)):
            original = getattr(self.state, name)
            setattr(self.state, name, fake)
            self.addCleanup(setattr, self.state, name, original)
        status, _ = self.post_audio(wav_bytes(), headers={"X-Want-Voice": "1"})
        self.assertEqual(status, 200)
        self.assertTrue(captured.get("want_voice"), captured)

    def test_no_token_is_401(self):
        request = urllib.request.Request(self.base + "/api/audio", data=wav_bytes(),
                                         method="POST",
                                         headers={"Content-Type": "audio/wav"})
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("should be unauthorized")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)


class TestAudioLeavesNoWaveformInMemory(AudioCase):
    def test_memory_gets_the_sentence_not_the_audio(self):
        self.stt.text = "我喜欢薄荷"
        status, data = self.prepare(wav_bytes())
        self.assertEqual(status, 200)
        self.state.send_stream(**data) and None
        for frame in self.state.send_stream(**data):
            self.assertNotIn("audio", str(frame[1])[:400].lower())
        facts = self.state.app.memory.snapshot({"owner"})["facts"]
        texts = " ".join(str(fact["text"]) for fact in facts)
        self.assertIn("薄荷", texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
