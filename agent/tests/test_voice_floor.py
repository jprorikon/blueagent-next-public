#!/usr/bin/env python3
"""语音发言权：通话持有 floor，force 语音按 turn 申请/释放。"""

from __future__ import annotations

import time

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.entities import build_entities
from agent.brain.runtime.voice import VoiceService


class TestVoiceFloor(unittest.TestCase):
    def test_call_holds_floor_until_hangup(self):
        voice = VoiceService(build_entities())
        result = voice.call("mira", end="discord", principal="owner")
        self.assertTrue(result["ok"])
        session_id = result["session"]["id"]
        self.assertEqual(voice.floor.holder("mira"), session_id)
        voice.hangup("mira")
        self.assertIsNone(voice.floor.holder("mira"))

    def test_force_voice_reports_floor_busy(self):
        voice = VoiceService(build_entities())
        self.assertTrue(voice.floor.acquire("mira", "someone-else", priority=0))
        frames = voice.maybe_speak("mira", "你好", force=True,
                                   target="discord:vc-lobby", thread_id="th_1")
        self.assertEqual(frames[0]["kind"], "speech_error")
        self.assertEqual(frames[0]["payload"]["reason"], "floor_busy")
        voice.floor.release("mira", "someone-else")

    def test_force_voice_acquires_and_releases_turn_floor(self):
        voice = VoiceService(build_entities())
        frames = voice.maybe_speak("mira", "你好", force=True,
                                   target="discord:vc-lobby", thread_id="th_1")
        self.assertEqual(frames[0]["kind"], "speech")
        self.assertEqual(voice.floor.holder("mira"), "th_1")
        voice.release_floor("mira", "th_1")
        self.assertIsNone(voice.floor.holder("mira"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
