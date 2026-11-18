#!/usr/bin/env python3
"""主人此刻方不方便被打擾（docs/PLAN-common-ground-20261109.md §5.2，第 8 步）。

判據：通話中／正在別處聊（10 分鐘內）⇒ 主動消息這一跳不說；信號壞了當沒有。
主人說「おやすみ」**不算**（owner 2026-11-09：她們有自己的生物鐘，主人睡了是主人自己的靜默）。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.interruptibility import make_owner_busy
from agent.brain.runtime.proactive import ProactiveGate

NOW = 1_800_000_000.0


def thread(entity, scope, turns):
    return NS(key=NS(entity=entity, scope=scope),
              transcript=[NS(speaker=speaker, text=text, at=at) for speaker, text, at in turns])


class Threads:
    def __init__(self, rows=(), owner_lines=None) -> None:
        self.rows = list(rows)
        self.lines = owner_lines or {}

    def threads(self):
        return list(self.rows)

    def recent_owner_lines(self, entity, limit=3, *, owners=("owner",)):
        return list(self.lines.get(entity, []))[-limit:]


class Voice:
    def __init__(self, sessions=None) -> None:
        self.sessions = sessions or {}

    def active(self, entity):
        return self.sessions.get(entity)


class TestBusy(unittest.TestCase):
    def busy(self, threads=None, voice=None):
        return make_owner_busy(threads=threads or Threads(), voice=voice,
                               entities=("mira", "aster"), clock=lambda: NOW, elsewhere_s=600)

    def test_free(self) -> None:
        self.assertEqual(self.busy()("mira"), "")

    def test_in_call_with_anyone(self) -> None:
        voice = Voice({"aster": NS(principal="owner")})
        self.assertEqual(self.busy(voice=voice)("mira"), "owner_in_call")
        guest_call = Voice({"aster": NS(principal="alice")})
        self.assertEqual(self.busy(voice=guest_call)("mira"), "", "客人在通話不算主人")

    def test_goodbye_is_not_a_reason(self) -> None:
        threads = Threads(owner_lines={"mira": [{"text": "おやすみ", "at": NOW - 3600}]})
        self.assertEqual(self.busy(threads)("mira"), "", "主人睡了是主人自己的靜默，她照自己的作息")

    def test_busy_elsewhere(self) -> None:
        threads = Threads([
            thread("mira", "owner", [("owner", "雨だね", NOW - 60)]),
            thread("aster", "owner", [("owner", "表どう？", NOW - 120)]),
        ])
        self.assertEqual(self.busy(threads)("mira"), "owner_busy_elsewhere", "正在跟 aster 聊")
        self.assertEqual(self.busy(threads)("aster"), "owner_busy_elsewhere", "對 aster 來說，ミラ那條是別處")
        group = Threads([thread("mira", "group:c1", [("owner", "みんなおはよう", NOW - 300)])])
        self.assertEqual(self.busy(group)("mira"), "owner_busy_elsewhere", "群裡正在說")
        own = Threads([thread("mira", "owner", [("owner", "雨だね", NOW - 60)])])
        self.assertEqual(self.busy(own)("mira"), "", "跟她自己的私聊不算別處")
        old = Threads([thread("aster", "owner", [("owner", "表どう？", NOW - 1800)])])
        self.assertEqual(self.busy(old)("mira"), "", "半小時前的不算正在聊")


class TestGateHook(unittest.TestCase):
    def test_gate_uses_interrupt(self) -> None:
        gate = ProactiveGate(min_gap_s=0, daily_max=99, quiet_start=0, quiet_end=0,
                             clock=lambda: NOW)
        gate.interrupt = lambda entity, principal, now: "owner_in_call"
        self.assertEqual(gate.allows("mira"), (False, "owner_in_call"))
        gate.interrupt = lambda entity, principal, now: (_ for _ in ()).throw(RuntimeError())
        self.assertEqual(gate.allows("mira"), (True, "ok"), "信號壞了當沒有")
        gate.interrupt = None
        self.assertEqual(gate.allows("mira"), (True, "ok"))

    def test_wire_switch(self) -> None:
        import os
        from unittest import mock

        from agent.brain.compose.interruptibility import wire

        app = NS(proactive=ProactiveGate(), threads=Threads(), voice=None,
                 entities=NS(all=lambda: [NS(id="mira")]))
        wire(app)
        self.assertIsNotNone(app.proactive.interrupt)
        with mock.patch.dict(os.environ, {"BLUEAGENT_OWNER_INTERRUPTIBILITY": "0"}):
            wire(app)
        self.assertIsNone(app.proactive.interrupt)


class TestEmotionGrounding(unittest.TestCase):
    """§5.1／§5.3：看不見原因的低落不說明就不露；在等回音時不算寂寞。"""

    def beat(self, *, label="しょんぼり", social_need=0.0, unanswered=(), compose=None):
        from agent.brain.runtime.heartbeat import Heartbeat

        class Affect:
            def advance(self, entity): return {}
            def snapshot(self, entity): return {"arousal": 0.5}
            def timeline(self, entity, limit=20): return []
            def idle_s(self, entity): return 0.0
            def mood(self, entity, at=None):
                return {"valence": -0.3, "arousal": 0.5, "label": label, "propensity": 0.9,
                        "social_need": social_need, "idle_s": 0.0}

        class Gate:
            def __init__(self): self.frames = []
            def allows(self, entity, *, now, principal): return True, "ok"
            def unanswered(self, entity, *, principal="owner", now=None): return list(unanswered)
            def maybe_send(self, entity, text, *, principal, publish, now, urgency="", force=False):
                self.frames.append({"text": text, "urgency": urgency})
                return {"sent": True, "frame": {}}

        gate = Gate()
        beat = Heartbeat(entity="mira", affect=Affect(), gate=gate, threshold=0.35,
                         clock=lambda: NOW, compose=compose)
        beat.arousal_pacing = False
        beat.tick()
        return gate

    def test_low_mood_template_has_no_unexplained_sulk(self) -> None:
        from agent.brain.runtime.heartbeat import _TEMPLATES

        gate = self.beat(label="しょんぼり")
        self.assertEqual(len(gate.frames), 1)
        self.assertNotIn("ちょっとだけ、話してもいい", gate.frames[0]["text"])
        self.assertTrue(gate.frames[0]["text"].startswith(_TEMPLATES["ふつう"][0]))

    def test_mood_note_asks_for_the_cause(self) -> None:
        from agent.brain.compose.heartbeat_compose import mood_note

        self.assertIn("理由を一言添える", mood_note("しょんぼり"))
        self.assertIn("オーナーのせいにしない", mood_note("いらいら"))
        self.assertEqual(mood_note("ごきげん"), "（いまの気分：ごきげん）")
        self.assertEqual(mood_note(""), "")

    def test_waiting_is_not_lonely(self) -> None:
        compose = lambda entity, mood, due=(), intent=None: "雨が降ってきたね"   # noqa: E731
        lonely = self.beat(label="ふつう", social_need=0.9, compose=compose)
        self.assertEqual(lonely.frames[0]["urgency"], "lonely")
        waiting = self.beat(label="ふつう", social_need=0.9, unanswered=[NOW - 7 * 3600],
                            compose=compose)
        self.assertEqual(waiting.frames[0]["urgency"], "", "在等回音 ⇒ 不是寂寞")


if __name__ == "__main__":
    unittest.main(verbosity=2)
