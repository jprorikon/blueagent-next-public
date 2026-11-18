#!/usr/bin/env python3
"""B③ followup 不重播開頭 ＋ B④ 語音房逐句播放。

③ 語音「先短回一句 → 再補完整答案」：第二段的 prompt 要帶上剛剛說過的那句，
   並要求「只補新內容」；`ack` 那輪不寫 transcript（原本就是），所以不會變成兩輪重複。
④ VC 裡每合成好一句就排進播放佇列（首句延遲＝第一句生成＋第一句合成），
   文字源仍合併成一個檔（附件只能整檔）。
"""

from __future__ import annotations

import base64
import io
import time
import unittest
import wave

from demo_common import *  # noqa: F401,F403

from agent.adapter.discord_source import DiscordConfig, DiscordSource
from agent.brain.messages import build_messages
from agent.brain.ports import BrainRequest

OWNER_ID = "100000000000000001"


def config(**overrides) -> DiscordConfig:
    values = {"DISCORD_BOT_TOKEN": "test-token", "GUILD_ID": "g1",
              "TEXT_CHANNEL_ID": "c1", "ALLOWED_USER_IDS": OWNER_ID}
    values.update(overrides)
    return DiscordConfig.from_env(values)


def wav_b64(seconds: float = 0.2, rate: int = 32000) -> str:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x01" * int(rate * seconds))
    return base64.b64encode(buffer.getvalue()).decode()


class TestFollowupPrompt(unittest.TestCase):
    def request(self, **meta) -> BrainRequest:
        base = {"transcript": [], "speak_lang": ""}
        base.update(meta)
        return BrainRequest(thread_id="t", entity="mira", principal="owner",
                            text="明日の件、調べて", system="persona", meta=base)

    def test_followup_tells_her_not_to_repeat_the_ack(self):
        messages = build_messages(self.request(followup=True,
                                               ack_text="おかわり、ちょっと待っててね。"))
        current = str(messages[-1]["content"])
        self.assertIn("おかわり、ちょっと待っててね。", current, "要帶上剛剛那句")
        self.assertIn("新しい内容だけ", current)
        self.assertIn("挨拶し直さない", current)

    def test_plain_turn_has_no_such_block(self):
        messages = build_messages(self.request())
        current = str(messages[-1]["content"])
        self.assertNotIn("新しい内容だけ", current)

    def test_ack_turn_is_not_written_twice(self):
        """短回覆那輪（quick）不寫使用者輪 ⇒ 同一句話不會在歷史裡出現兩次。"""
        state = make_state()
        self.addCleanup(state.shutdown)
        state.send("テスト", principal="owner", quick=True)
        from agent.brain.domain.ids import ThreadKey
        key = ThreadKey(scope="owner", entity="mira", scene="together", purpose="work")
        thread = state.app.threads.current_for(key)
        rows = [row for row in (thread.transcript if thread else []) if row.role == "user"]
        self.assertLessEqual(len(rows), 1, f"quick 那輪不該寫使用者輪：{rows}")


class FakeState:
    """只實作 `handle()` 在 VC-work 這條路上會用到的三個方法。"""

    def __init__(self, frames):
        self.frames = frames
        self.sent: list[dict] = []

    def send_voice(self, text, **kwargs):
        self.sent.append({"text": text, **kwargs})
        return {"frames": self.frames, "following_up": False}

    def reply_text(self, result):
        return "はい、わかりました。"

    def send_stream(self, **kwargs):
        # 文字源那條路：memory 幀 + 兩個 speech 幀 + done
        yield "frame", {"kind": "memory", "payload": {"hits": 1}}
        for frame in self.frames:
            yield "frame", frame
        yield "frame", {"kind": "done", "payload": {"text": "はい、わかりました。"}}
        yield "result", {"observed": False, "decision": {"engage": "reply"}}


class TestStreamPlay(unittest.TestCase):
    def setUp(self):
        self.frames = [{"kind": "speech", "payload": {"audio": wav_b64(0.1)}},
                       {"kind": "speech", "payload": {"audio": wav_b64(0.1)}}]
        self.played: list[bytes] = []
        played = self.played

        class FakeVoice:
            def publish_audio(inner, audio):        # noqa: N805
                played.append(audio)
                return True

        self.voice = FakeVoice()

    def _source(self) -> DiscordSource:
        source = DiscordSource(config(), FakeState(self.frames))
        source.voice = self.voice
        return source

    def test_vc_reply_is_streamed_per_sentence(self):
        result = self._source().handle("これ調べて", OWNER_ID, guild=True, mentioned=False,
                                       is_dm=False, channel_id="c1", force_addressed=True)
        self.assertEqual(len(self.played), 2, "兩句要分兩次排進播放佇列")
        self.assertEqual(result.get("streamed"), 2)
        self.assertFalse(result.get("audio"), "已經逐句播掉 ⇒ 不要再回一個合併檔（會播兩次）")

    def test_text_source_still_merges_into_one_file(self):
        result = self._source().handle("これは何？", OWNER_ID, guild=True, mentioned=False,
                                       is_dm=False, channel_id="c1")
        self.assertEqual(self.played, [], "文字頻道不逐句播（附件只能整檔）")
        self.assertTrue(result.get("audio"), "文字頻道要拿到合併後的 wav")
        self.assertEqual(result.get("clips"), 2)

    def test_followup_frames_stream_via_hook(self):
        """後續完整答案那一步真的把幀交出來（`on_speech_frame` → `send(on_frame=…)`）。"""
        import time
        state = make_state()
        self.addCleanup(state.shutdown)
        seen: list[dict] = []
        submitted = state.commands.submit(
            "これ調べて", entity="mira", principal="owner", source="test",
            meta={"on_speech_frame": lambda frame: seen.append(frame),
                  "want_voice": False})
        self.assertTrue(submitted.get("ok"), submitted)
        deadline = time.time() + 5
        while time.time() < deadline and not seen:
            time.sleep(0.05)
        self.assertTrue(seen, "後續工作應該把幀交出來（否則逐句播放沒有原料）")

    def test_only_speech_frames_are_played(self):
        """`publish_speech_frame` 只認 speech＋audio；其他幀一律不理（不然會亂播）。"""
        source = DiscordSource(config(), FakeState(self.frames))
        source.voice = self.voice
        self.assertFalse(source.publish_speech_frame({"kind": "delta", "payload": {"text": "…"}}))
        self.assertFalse(source.publish_speech_frame({"kind": "speech", "payload": {}}))
        self.assertTrue(source.publish_speech_frame(
            {"kind": "speech", "payload": {"audio": wav_b64(0.1)}}))
        self.assertEqual(len(self.played), 1)

    def test_stream_can_be_disabled(self):
        import os
        os.environ["BLUEAGENT_VC_STREAM_PLAY"] = "0"
        try:
            result = self._source().handle("これ調べて", OWNER_ID, guild=True,
                                           mentioned=False, is_dm=False,
                                           channel_id="c1", force_addressed=True)
        finally:
            os.environ.pop("BLUEAGENT_VC_STREAM_PLAY", None)
        self.assertEqual(self.played, [], "關掉就回到合併那條")
        self.assertTrue(result.get("audio"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
