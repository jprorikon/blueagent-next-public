#!/usr/bin/env python3
"""訪客端的權限邊界（owner 2026-11-02：「做掉，然後让 guest 可以发图片和语音信息，只是不允许通话」）。

這一支守四條：
  ① **guest 送圖、送語音可以**（2026-11-02 之前 `prepare_image` 用一個不存在的
     `auth.can_send` 判 ⇒ `hasattr=False` ⇒ 判式恆真 ⇒ guest 送照片一律 403）；
  ② **通話不給 guest**：`/api/voice/*` 全部 owner-only；`/api/audio` 帶 `X-Call` ⇒ 403
     （掛在「是不是主人」這一關，不是另一套權限）；
  ③ **她的內心與記憶只給主人**：`/api/memory/windows`、`/api/memory/trace`、`/api/self/next`
     從前只檢查「有沒有登入」⇒ guest token 打得到，現在一律 403；
  ④ **`/api/state` 不要誤收**：App 的狀態頁兩端都用它（guest 拿自己的 principal 快照），
     它必須繼續對 guest 開著。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

import agent.server.api as api_module
from agent.adapter.stt_local import Transcript
from agent.brain.domain.auth import AuthContext, Role
from agent.brain.media.store import MediaStore

# 1x1 PNG（最小的合法圖檔，不需要 Pillow）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000001e221bc330000000049454e44ae"
    "426082")

OWNER = AuthContext(principal="owner", role=Role.OWNER, capabilities=frozenset({"chat"}))
GUEST = AuthContext(principal="guest-x", role=Role.GUEST, capabilities=frozenset({"chat"}))


class FakeSTT:
    """可控的轉寫器（形狀同 `tests/test_audio_input.py`，不吃音檔內容）。"""

    def __init__(self, text="写真を送ったよ。"):
        self.text = text
        self.calls: list[int] = []

    def transcribe(self, audio, *, language="", suffix=".wav"):
        self.calls.append(len(audio))
        return Transcript(text=self.text, language="ja", probability=0.95,
                          seconds=1.5, engine="fake")

    def pcm16_to_wav(self, pcm, *, rate=16000, channels=1):
        return b"RIFF" + bytes(len(pcm))


class GuestCase(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.state.media = MediaStore(pathlib.Path(tempfile.mkdtemp(prefix="ba-media-")))
        self.stt = FakeSTT()
        self.state.app.stt = self.stt

    def image(self, *, auth=GUEST):
        return api_module.prepare_image(
            self.state, PNG,
            headers={"Content-Type": "image/png", "X-Entity": "mira", "X-Note": ""},
            auth=auth)

    def audio(self, *, auth=GUEST, headers=None):
        return api_module.prepare_audio(
            self.state, b"RIFF" + b"\x00\x00" * 1600, headers=dict(headers or {}), auth=auth)


class TestGuestCanSendImageAndVoice(GuestCase):
    def test_guest_can_send_image(self):
        status, payload = self.image()
        self.assertEqual(status, 200, payload)
        # 管線那份要帶 path（看得見圖），端末那份不含 path
        self.assertTrue(payload["media"].get("path"), "管線要拿到檔案路徑才讀得到圖")
        self.assertNotIn("path", payload["media_public"])
        self.assertEqual(payload["principal"], "guest-x")

    def test_guest_can_send_voice_but_never_gets_voice_back(self):
        status, payload = self.audio(headers={"X-Want-Voice": "1", "X-Mode": "work"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["text"], "写真を送ったよ。")
        # guest 一律不出聲、一律 chat（委任只有主人能開）
        self.assertFalse(payload["want_voice"])
        self.assertEqual(payload["mode"], "chat")
        self.assertEqual(payload["mode_note"], "work_requires_owner")

    def test_owner_image_still_works(self):
        status, payload = self.image(auth=OWNER)
        self.assertEqual(status, 200, payload)


class TestGuestCannotCall(GuestCase):
    def test_call_header_is_forbidden_for_guest(self):
        status, payload = self.audio(headers={"X-Call": "call-1"})
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"], "forbidden")
        self.assertEqual(self.stt.calls, [], "擋在轉寫之前，不該先花一次識別")

    def test_voice_endpoints_are_owner_only_for_guest(self):
        for action in ("call", "ping", "interrupt", "hangup"):
            status, payload = api_module.dispatch_post(
                self.state, f"/api/voice/{action}", {}, auth=GUEST)
            self.assertEqual(status, 403, f"/api/voice/{action} 對 guest 應該是 403：{payload}")
            self.assertEqual(payload["error"], "forbidden")


class TestHerInnerWorldIsOwnerOnly(GuestCase):
    def test_memory_windows_is_forbidden_for_guest(self):
        status, payload = api_module.get_memory_windows(
            self.state, {"entity": ["mira"]}, auth=GUEST)
        self.assertEqual(status, 403, payload)
        self.assertEqual(api_module.get_memory_windows(
            self.state, {"entity": ["mira"]}, auth=OWNER)[0], 200)

    def test_memory_trace_is_forbidden_for_guest(self):
        status, payload = api_module.get_memory_trace(self.state, {"q": ["池袋"]}, auth=GUEST)
        self.assertEqual(status, 403, payload)
        self.assertEqual(api_module.get_memory_trace(
            self.state, {"q": ["池袋"]}, auth=OWNER)[0], 200)

    def test_self_task_is_forbidden_for_guest(self):
        status, payload = api_module.get_self_task(
            self.state, {"entity": ["mira"]}, auth=GUEST)
        self.assertEqual(status, 403, payload)

    def test_mood_says_403_not_401_to_guest(self):
        """401 的意思是「回登入頁」；guest 看不到她的心情是 403（契約 §4）。"""
        status, payload = api_module.get_mood(
            self.state, {"entity": ["mira"]}, auth=GUEST)
        self.assertEqual(status, 403, payload)
        self.assertEqual(api_module.get_mood(self.state, {}, auth=None)[0], 401)

    def test_anonymous_is_still_401_not_403(self):
        for call in (lambda: api_module.get_memory_windows(self.state, {}, auth=None),
                     lambda: api_module.get_memory_trace(self.state, {"q": ["x"]}, auth=None),
                     lambda: api_module.get_self_task(self.state, {}, auth=None)):
            self.assertEqual(call()[0], 401, "沒帶 token 是 401（端末要回登入頁），不是 403")


class TestStateStaysOpenToGuest(GuestCase):
    def test_guest_can_read_own_state(self):
        status, payload = api_module.get_state(
            self.state, {"entity": ["mira"]}, auth=GUEST)
        self.assertEqual(status, 200, payload)
        self.assertNotIn("affect", payload, "情緒與心跳只給主人")

    def test_guest_state_omits_hidden_memory_text(self):
        secret = "主人的私密安排：周末回老家。"
        self.state.app.memory.add(secret, layer="pair",
                                  audience={"mira", "owner"}, principal="owner",
                                  entity="mira")
        self.state.app.memory.add("窗边有一盆薄荷。", layer="world", entity="mira")

        status, payload = api_module.get_state(
            self.state, {"entity": ["mira"]}, auth=GUEST)
        self.assertEqual(status, 200, payload)
        facts = payload["memory"]["facts"]
        self.assertNotIn(secret, {fact["text"] for fact in facts})
        self.assertIn("窗边有一盆薄荷。", {fact["text"] for fact in facts})
        self.assertTrue(all(fact["visible"] for fact in facts))
        self.assertGreaterEqual(payload["memory"]["hidden_counts"].get("pair", 0), 1)

        status, owner_payload = api_module.get_state(
            self.state, {"entity": ["mira"]}, auth=OWNER)
        self.assertEqual(status, 200, owner_payload)
        self.assertIn(secret, {fact["text"] for fact in owner_payload["memory"]["facts"]})


if __name__ == "__main__":
    unittest.main()
