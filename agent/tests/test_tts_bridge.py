#!/usr/bin/env python3
"""真聲音的嘴（包現役橋 ba/voice）：離線驗收。

不打真 TTS：起本地 HTTP 假端點（`/tts` 契約與 GSV 相同），驗
  ① 用的是橋的分句（`split_for_speech`，Sentences.kt 孿生）；
  ② clip 的形狀（audio/seconds/motion/shape/instance）；
  ③ 聲線別名（lumi→mira）與池路由；
  ④ 失敗語義（TtsError → VoiceService 出 speech_error，文字不吞）；
  ⑤ `VoiceService` 接線（真幀 mock=False、seq 遞增）。

橋本身不在（`BLUEAGENT_TTS_BRIDGE_ROOT` 沒指到含 `ba/voice` 的樹）⇒ 相關 case 跳過。
"""

from __future__ import annotations

import base64
import io
import json
import os
import pathlib
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403

from agent.adapter.tts_bridge import (BridgeConfig, BridgeTts, TtsError,
                                                build_tts, load_bridge, tts_label,
                                                wav_seconds)
from agent.brain.compose.entities import build_entities
from agent.brain.runtime.voice import VoiceService

RATE = 32000
SECONDS = 0.4
BRIDGE_ROOT = os.environ.get("BLUEAGENT_TTS_BRIDGE_ROOT", "").strip()


def bridge_ready() -> bool:
    return bool(BRIDGE_ROOT) and (pathlib.Path(BRIDGE_ROOT) / "ba" / "voice" / "tts.py").exists()


def make_wav(seconds: float = SECONDS, rate: int = RATE) -> bytes:
    import math
    frames = int(seconds * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        payload = bytearray()
        for index in range(frames):
            value = int(6000 * math.sin(2 * math.pi * 440 * index / rate))
            payload += value.to_bytes(2, "little", signed=True)
        handle.writeframes(bytes(payload))
    return buffer.getvalue()


class FakeTts(BaseHTTPRequestHandler):
    fail_status = 0
    requests: list[dict] = []

    def do_POST(self):                                     # noqa: N802 - http.server 介面
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        type(self).requests.append({"path": self.path, "body": body})
        if type(self).fail_status:
            self.send_response(type(self).fail_status)
            self.end_headers()
            self.wfile.write(b'{"message":"tts failed"}')
            return
        audio = make_wav()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def do_GET(self):                                      # noqa: N802
        if self.path == "/docs":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)

    def log_message(self, *_args):
        return None


@unittest.skipUnless(bridge_ready(), "橋（ba/voice）不在：設 BLUEAGENT_TTS_BRIDGE_ROOT 再跑")
class BridgeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTts)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.assets = pathlib.Path(tempfile.mkdtemp(prefix="tts-assets-"))
        refs = cls.assets / "voices" / "mira"
        refs.mkdir(parents=True)
        (refs / "refs.json").write_text(json.dumps({
            "character": "mira", "language": "ja",
            "refs": [{"file": "a.wav", "label": "greeting", "jp": "テスト"},
                     {"file": "b.wav", "label": "entrance", "jp": "じゃーん"}],
        }, ensure_ascii=False), encoding="utf-8")
        (refs / "meta.json").write_text(json.dumps({
            "character": "mira", "language": "ja", "default_ref": "b.wav",
        }, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeTts.requests = []
        FakeTts.fail_status = 0

    def tts(self, **kwargs) -> BridgeTts:
        config = BridgeConfig(bridge_root=pathlib.Path(BRIDGE_ROOT), assets=self.assets,
                              url=kwargs.pop("url", self.url),
                              remote_root=kwargs.pop("remote_root", "/home/developer/gsv-voices"),
                              motion=kwargs.pop("motion", False), **kwargs)
        return BridgeTts(config)


class TestBridgeLoad(BridgeCase):
    def test_load_bridge_exposes_split_and_say(self):
        tts, pool = load_bridge(BRIDGE_ROOT)
        self.assertTrue(callable(tts.split_for_speech))
        self.assertTrue(callable(tts.say))
        self.assertTrue(hasattr(pool, "from_env"))

    def test_missing_root_is_loud(self):
        with self.assertRaises(TtsError):
            load_bridge("/nonexistent/bridge-root")


class TestSpeak(BridgeCase):
    def test_uses_bridge_splitter(self):
        clips = self.tts().speak("おはよう。元気？", "mira")
        self.assertEqual([clip.text for clip in clips], ["おはよう。", "元気？"])
        self.assertEqual([clip.index for clip in clips], [0, 1])
        self.assertTrue(clips[-1].last)
        self.assertFalse(clips[0].last)

    def test_clip_carries_real_wav_and_contract_payload(self):
        clip = self.tts().speak("おはよう。", "mira")[0]
        self.assertTrue(clip.wav.startswith(b"RIFF"))
        self.assertAlmostEqual(clip.seconds, SECONDS, places=2)
        payload = clip.payload()
        self.assertTrue(base64.b64decode(payload["audio"]).startswith(b"RIFF"))
        self.assertEqual(payload["shape"], "mira")
        self.assertEqual(payload["index"], 0)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["motion"], [])
        # 送給 GSV 的是橋的 payload 契約（欄位齊全、參考音走遠端路徑）
        body = FakeTts.requests[0]["body"]
        for key in ("text", "text_lang", "ref_audio_path", "prompt_text", "prompt_lang",
                    "media_type", "speed_factor", "seed", "text_split_method",
                    "batch_size", "streaming_mode"):
            self.assertIn(key, body)
        self.assertEqual(body["ref_audio_path"], "/home/developer/gsv-voices/mira/refs/b.wav")
        self.assertEqual(FakeTts.requests[0]["path"], "/tts")

    def test_alias_keeps_shape_but_borrows_voice(self):
        tts = self.tts(aliases={"lumi": "mira"})
        clip = tts.speak("おはよう。", "lumi")[0]
        self.assertEqual(clip.shape, "lumi", "嘴型 shape 仍是原角色")
        self.assertEqual(FakeTts.requests[0]["body"]["ref_audio_path"],
                         "/home/developer/gsv-voices/mira/refs/b.wav")

    def test_empty_text_speaks_nothing(self):
        self.assertEqual(self.tts().speak("   ", "mira"), [])

    def test_http_error_becomes_tts_error(self):
        FakeTts.fail_status = 500
        with self.assertRaises(TtsError):
            self.tts().speak("だめ", "mira")

    def test_unreachable_becomes_tts_error(self):
        with self.assertRaises(TtsError):
            self.tts(url="http://127.0.0.1:9", timeout_s=2.0).speak("だめ", "mira")

    def test_pool_routes_and_records_instance(self):
        tts = self.tts(pool=f"{self.url}|/home/developer/gsv-voices")
        clip = tts.speak("おはよう。", "mira")[0]
        self.assertEqual(clip.instance, self.url)
        health = tts.health()
        self.assertEqual(health["backend"], "bridge")
        self.assertEqual(len(health["instances"]), 1)
        self.assertEqual(health["instances"][0]["hot"], "mira")

    def test_per_character_url_overrides_default(self):
        tts = self.tts(url="http://127.0.0.1:9",
                       urls={"mira": self.url})
        clip = tts.speak("おはよう。", "mira")[0]
        self.assertTrue(clip.wav.startswith(b"RIFF"))


class TestVoiceServiceWiring(BridgeCase):
    def test_real_tts_frames_carry_audio_and_are_not_mock(self):
        voice = VoiceService(build_entities(), tts=self.tts())
        frames = voice.maybe_speak("mira", "おはよう。元気？", force=True,
                                   target="phone:local", thread_id="th_1",
                                   principal="owner")
        self.assertEqual([frame["kind"] for frame in frames], ["speech", "speech"])
        first = frames[0]["payload"]
        self.assertFalse(first["mock"])
        self.assertTrue(base64.b64decode(first["audio"]).startswith(b"RIFF"))
        self.assertGreater(first["duration_ms"], 0)
        self.assertEqual(first["voice"], "mira")
        self.assertEqual(first["thread_id"], "th_1")
        self.assertIn("motion", first)
        self.assertEqual([frame["payload"]["seq"] for frame in frames],
                         sorted(frame["payload"]["seq"] for frame in frames))
        self.assertFalse(voice.snapshot()["last_speech"]["mock"])

    def test_tts_failure_keeps_text_and_reports_speech_error(self):
        FakeTts.fail_status = 503
        voice = VoiceService(build_entities(), tts=self.tts())
        frames = voice.maybe_speak("mira", "だめ", force=True,
                                   target="phone:local", thread_id="th_1")
        self.assertEqual(frames[0]["kind"], "speech_error")
        self.assertIn("tts_failed", frames[0]["payload"]["reason"])
        self.assertEqual(frames[0]["payload"]["text"], "だめ")

    def test_no_tts_still_mock(self):
        voice = VoiceService(build_entities())
        frames = voice.maybe_speak("mira", "你好", force=True,
                                   target="phone:local", thread_id="th_1")
        self.assertTrue(frames[0]["payload"]["mock"])


class TestWavAndEnv(unittest.TestCase):
    def test_wav_seconds_reads_header(self):
        self.assertAlmostEqual(wav_seconds(make_wav(0.5)), 0.5, places=2)

    def test_wav_seconds_falls_back_on_garbage(self):
        self.assertAlmostEqual(wav_seconds(b"\x00" * 64000), 1.0, places=2)

    def test_off_by_default(self):
        self.assertIsNone(build_tts({}))
        self.assertEqual(tts_label(None), "mock")

    def test_missing_root_or_url_is_loud(self):
        with self.assertRaises(ValueError):
            build_tts({"BLUEAGENT_TTS": "bridge", "TTS_URL": "http://x", "TTS_ASSETS": "/a"})
        with self.assertRaises(ValueError):
            build_tts({"BLUEAGENT_TTS": "bridge", "BLUEAGENT_TTS_BRIDGE_ROOT": "/b",
                       "TTS_ASSETS": "/a"})

    def test_builds_from_env_with_bridge_variable_names(self):
        tts = build_tts({"BLUEAGENT_TTS": "bridge",
                         "BLUEAGENT_TTS_BRIDGE_ROOT": "/bridge",
                         "TTS_ASSETS": "/a",
                         "TTS_URL": "http://127.0.0.1:9881",
                         "TTS_URLS": "mira=http://127.0.0.1:9882",
                         "VOICE_ALIASES": "lumi=mira",
                         "TTS_TEXT_LANG": "ja"})
        self.assertIsNotNone(tts)
        self.assertEqual(tts.config.text_lang, "ja")
        self.assertEqual(tts.url_for("mira"), "http://127.0.0.1:9882")
        self.assertEqual(tts.voice_for("lumi"), "mira")
        self.assertIn("bridge", tts_label(tts))

    def test_text_lang_defaults_to_locked_ja(self):
        tts = build_tts({"BLUEAGENT_TTS": "bridge",
                         "BLUEAGENT_TTS_BRIDGE_ROOT": "/bridge",
                         "TTS_ASSETS": "/a", "TTS_URL": "http://127.0.0.1:9881"})
        self.assertEqual(tts.config.text_lang, "ja", "TTS_TEXT_LANG 未設＝鎖 ja")

    def test_auto_is_rejected_now(self):
        with self.assertRaises(ValueError):
            build_tts({"BLUEAGENT_TTS": "bridge",
                       "BLUEAGENT_TTS_BRIDGE_ROOT": "/bridge",
                       "TTS_ASSETS": "/a", "TTS_URL": "http://127.0.0.1:9881",
                       "TTS_TEXT_LANG": "auto"})

    def test_lang_for_never_drifts(self):
        for configured in ("ja", "zh", "", "klingon"):
            config = BridgeConfig(bridge_root=pathlib.Path("/bridge"),
                                  assets=pathlib.Path("/a"), text_lang=configured)
            tts = BridgeTts(config)
            for sentence in ("老师，晚安。", "おやすみ、オーナー。"):
                self.assertEqual(tts._lang_for(sentence),
                                 configured if configured in ("ja", "zh") else "ja")

    def test_unknown_backend_is_loud(self):
        with self.assertRaises(ValueError):
            build_tts({"BLUEAGENT_TTS": "sovits9000"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
