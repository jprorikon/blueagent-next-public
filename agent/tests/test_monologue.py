#!/usr/bin/env python3
"""內心獨白 v1：**每幾分鐘自己想一遍**（owner 2026-11-01「走下一步」；形狀照 aster）。

三條不變式（aster 2026-11-01 的 arch 文件，逐條照抄）：
  ① 送進模型的 facts **不含 v／a**（數值只以「心情定位的人話」出現）
  ② 模型回非 JSON 也要**留得住那一拍**（原文當獨白、action=none）
  ③ 節奏不是固定 5 分：階梯 0/5/5/10/10/20/30/60/120 分，**6 小時沒送出也算一次事件**

消費方在心跳（`heartbeat.py`）：`work` 開活動；`talk` 只當動機交給組句器，獨白原文不出門（common-ground 第 1 步）。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agent.brain.runtime.inner.monologue import (  # noqa: E402
    CADENCE_MIN, EVENT_GAP_S, Monologue,
)


class Clock:
    def __init__(self, start: float = 100_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeCall:
    def __init__(self, reply: str = '{"text": "さっきの話、もう少し聞いてみたいな。", "action": "none", "why": "気になる"}') -> None:
        self.reply = reply
        self.calls: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.reply


class TestMonologue(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ba-inner-")
        self.clock = Clock()
        self.call = FakeCall()
        self.logs: list[str] = []
        self.mono = Monologue(self.root, call=self.call, clock=self.clock, enabled=True,
                              log=self.logs.append)

    def test_first_beat_is_due_and_writes(self) -> None:
        self.assertTrue(self.mono.due("mira"))
        row = self.mono.beat("mira", facts={"mood": "少し元気", "activities": ["調べもの"]})
        self.assertEqual(row["action"], "none")
        self.assertTrue(row["parsed"])
        lines = (pathlib.Path(self.root) / "mira.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "さっきの話、もう少し聞いてみたいな。")

    def test_facts_never_carry_raw_affect(self) -> None:
        """不變式①：v／a 不能從 facts 的後門回到模型面前。"""
        self.mono.beat("mira", facts={"mood": "少し元気", "valence": 0.31,
                                         "arousal": 0.42, "affect": {"valence": 1},
                                         "v": 1, "a": 1})
        prompt = self.call.calls[0]
        self.assertNotIn("0.31", prompt)
        self.assertNotIn("0.42", prompt)
        self.assertIn("少し元気", prompt)

    def test_non_json_reply_still_kept(self) -> None:
        """不變式②：模型回散文也要留得住那一拍（原文當獨白、action=none）。"""
        self.call.reply = "今日は静かだな、と思った。"
        row = self.mono.beat("mira", facts={})
        self.assertFalse(row["parsed"])
        self.assertEqual(row["text"], "今日は静かだな、と思った。")
        self.assertEqual(row["action"], "none")
        self.assertTrue(self.mono.latest("mira"))

    def test_cadence_ladder(self) -> None:
        """不變式③：跑過一拍之後按階梯等（0→5→5→10…），不是固定 5 分。"""
        self.mono.beat("mira", facts={})
        self.assertAlmostEqual(self.mono.interval_s("mira"), CADENCE_MIN[1] * 60.0)
        self.assertFalse(self.mono.due("mira"), "剛跑完，不該馬上又跑")
        self.clock.tick(CADENCE_MIN[1] * 60.0 - 1)
        self.assertFalse(self.mono.due("mira"))
        self.clock.tick(2)
        self.assertTrue(self.mono.due("mira"), "到點就該跑")
        for _ in range(4):
            self.mono.beat("mira", facts={})
            self.clock.tick(self.mono.interval_s("mira"))
        self.assertGreaterEqual(self.mono.interval_s("mira"), CADENCE_MIN[4] * 60.0)

    def test_six_hours_without_talk_counts_as_event(self) -> None:
        """6 小時沒送出 ⇒ 即使階梯還沒到也算一次事件（不然越跑越稀）。"""
        self.mono.beat("mira", facts={})
        for _ in range(8):                                # 推到最稀那一格
            self.mono.beat("mira", facts={})
            self.clock.tick(self.mono.interval_s("mira"))
        self.assertAlmostEqual(self.mono.interval_s("mira"), CADENCE_MIN[-1] * 60.0)
        self.clock.tick(EVENT_GAP_S - 1)
        state = self.mono.state("mira")
        state["at"] = self.clock.now            # 假裝剛剛才跑過 ⇒ 只剩「6 小時」這條會讓它 due
        self.mono._save_state("mira", state)
        self.assertFalse(self.mono.due("mira"))
        state["at"] = self.clock.now - EVENT_GAP_S - 1
        self.mono._save_state("mira", state)
        self.assertTrue(self.mono.due("mira"))

    def test_talk_marks_sent(self) -> None:
        self.call.reply = '{"text": "ちょっと話したい。", "action": "talk"}'
        row = self.mono.beat("mira", facts={})
        self.assertEqual(row["action"], "talk")
        self.assertTrue(self.mono.state("mira").get("sent_at"), "talk 要記「上次送出」給 6 小時那條判")

    def test_disabled_by_default(self) -> None:
        env = os.environ.pop("BLUEAGENT_MONOLOGUE", None)
        try:
            mono = Monologue(self.root + "-off", call=self.call, clock=self.clock)
            self.assertFalse(mono.enabled, "程式預設關；要開是 compose 的事")
            self.assertFalse(mono.due("mira"))
            self.assertIsNone(mono.beat("mira", facts={}))
        finally:
            if env is not None:
                os.environ["BLUEAGENT_MONOLOGUE"] = env

    def test_failure_does_not_raise(self) -> None:
        def boom(_prompt):
            raise RuntimeError("upstream")

        mono = Monologue(self.root + "-boom", call=boom, clock=self.clock, enabled=True)
        self.assertIsNone(mono.beat("mira", facts={}))
        self.assertEqual(mono.failures, 1)



class StubMonologue:
    """回一個固定的獨白（用來驗「模型決定之後，系統照它做」）。"""

    def __init__(self, action: str, text: str = "ちょっと話したいんだ。") -> None:
        self.action = action
        self.text = text
        self.beats: list[str] = []

    def due(self, entity, *, at=None, scale=1.0) -> bool:   # 與 Monologue.due 同一個簽名
        return True

    def beat(self, entity, *, facts, at=None):
        self.beats.append(str(entity))
        self.facts = dict(facts)
        return {"text": self.text, "action": self.action, "why": "声が聞きたい", "parsed": True}

    def cached(self, entity):
        return {}


class StubAffect:
    def advance(self, entity):
        return {}

    def snapshot(self, entity):
        return {"arousal": 0.5}

    def mood(self, entity, at=None):
        return {"valence": 0.2, "arousal": 0.5, "label": "ごきげん",
                "propensity": 0.9, "social_need": 0.0, "idle_s": 0.0}

    def timeline(self, entity, limit=20):
        return []

    def idle_s(self, entity):
        return 0.0


class StubGate:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def allows(self, entity, *, now, principal):
        return True, "ok"

    def maybe_send(self, entity, text, *, principal, publish, now, urgency="", force=False):
        self.sent.append(str(text))
        return {"sent": True, "frame": {}}


class TestMonologueRouting(unittest.TestCase):
    """owner 2026-11-01：「獨白下一步接誰應該讓模型自己決定」⇒ 系統照 `action` 做，不自己挑。"""

    def _beat(self, action: str, **kwargs):
        from agent.brain.runtime.heartbeat import Heartbeat

        gate = StubGate()
        mono = StubMonologue(action)
        beat = Heartbeat(entity="mira", affect=StubAffect(), gate=gate, publish=None,
                         threshold=0.35, clock=lambda: 1000.0, monologue=mono, **kwargs)
        beat.arousal_pacing = False
        beat.tick()
        return gate, mono

    def test_talk_never_sends_monologue_text_by_default(self) -> None:
        """common-ground 第 1 步：獨白是內心話，主人沒看過 ⇒ 原文不出門（沒有 compose 就走模板）。"""
        gate, _mono = self._beat("talk", talk_route="compose")
        self.assertEqual(len(gate.sent), 1, "模型說想說話 ⇒ 這一跳照樣開口")
        self.assertNotIn("ちょっと話したいんだ。", gate.sent, "但送出去的不能是獨白原文")

    def test_talk_hands_monologue_to_compose_as_intent(self) -> None:
        seen: list[dict] = []

        def compose(entity, mood, due=(), intent=None):
            seen.append(intent)
            return "いま作業の合間なんだけど、ちょっと声が聞きたくなって。"

        gate, _mono = self._beat("talk", talk_route="compose", compose=compose)
        self.assertEqual(seen, [{"text": "ちょっと話したいんだ。", "why": "声が聞きたい"}],
                         "獨白原文與理由只當動機交給組句器")
        self.assertEqual(gate.sent, ["いま作業の合間なんだけど、ちょっと声が聞きたくなって。"])

    def test_compose_without_intent_param_still_works(self) -> None:
        """舊的 `compose(entity, mood, due=…)` 不收 intent ⇒ 照舊組句，不退回獨白原文。"""
        def compose(entity, mood, due=()):
            return "ねえ、いま少しいい？"

        gate, _mono = self._beat("talk", talk_route="compose", compose=compose)
        self.assertEqual(gate.sent, ["ねえ、いま少しいい？"])

    def test_raw_route_keeps_old_behaviour(self) -> None:
        gate, _mono = self._beat("talk", talk_route="raw")
        self.assertEqual(gate.sent, ["ちょっと話したいんだ。"], "`raw` ⇒ 舊行為：獨白原文直接過閘")

    def test_env_selects_route(self) -> None:
        from unittest import mock
        from agent.brain.runtime.heartbeat import Heartbeat

        with mock.patch.dict("os.environ", {"BLUEAGENT_MONOLOGUE_TALK": "raw"}):
            beat = Heartbeat(entity="mira", affect=StubAffect(), gate=StubGate())
        self.assertEqual(beat.talk_route, "raw")
        with mock.patch.dict("os.environ", {"BLUEAGENT_MONOLOGUE_TALK": "whatever"}):
            beat = Heartbeat(entity="mira", affect=StubAffect(), gate=StubGate())
        self.assertEqual(beat.talk_route, "compose", "認不得的值 ⇒ 預設的安全那條")

    def test_monologue_sees_recent_talk(self) -> None:
        """以前 `talk` 寫死空字串 ⇒ 獨白看不到對話卻在決定要不要找主人說話。"""
        _gate, mono = self._beat("none", recent_talk=lambda entity: f"{entity}：オーナー「雨だね」")
        self.assertEqual(mono.facts.get("talk"), "mira：オーナー「雨だね」")

    def test_recent_talk_failure_does_not_break_beat(self) -> None:
        def broken(entity):
            raise RuntimeError("boom")

        _gate, mono = self._beat("none", recent_talk=broken)
        self.assertEqual(mono.facts.get("talk"), "")

    def test_none_does_not_borrow_monologue_text(self) -> None:
        """`action=none` ⇒ 獨白的話**不拿來當台詞**；心跳自己那條路照舊（它自己的台詞）。"""
        gate, mono = self._beat("none")
        self.assertEqual(mono.beats, ["mira"], "獨白照跑")
        self.assertNotIn("ちょっと話したいんだ。", gate.sent,
                         "沒說想說話就不能拿獨白那段去發")

    def test_work_actually_starts_an_activity(self) -> None:
        started: list[tuple[str, dict]] = []
        self._beat("work", on_self_work=lambda entity, row: started.append((entity, row)))
        self.assertEqual(len(started), 1, "action=work ⇒ 真的去做")
        self.assertEqual(started[0][0], "mira")


class TestComposeHelpers(unittest.TestCase):
    def test_intent_lines_marks_unseen(self) -> None:
        from agent.brain.compose.heartbeat_compose import intent_lines

        lines = intent_lines({"text": "観測表、まだ途中。", "why": "見てほしい"})
        self.assertEqual(len(lines), 1)
        self.assertIn("オーナーはこれを読んでいない", lines[0])
        self.assertIn("そのまま送らない", lines[0])
        self.assertIn("観測表、まだ途中。", lines[0])
        self.assertIn("見てほしい", lines[0])
        self.assertEqual(intent_lines(None), [])
        self.assertEqual(intent_lines({"text": "  "}), [])

    def test_compose_prompt_carries_intent(self) -> None:
        from agent.brain.compose.heartbeat_compose import make_heartbeat_composer

        class Brain:
            request = None

            def run_turn(self, request):
                Brain.request = request
                yield "done", {"text": "いま作業の合間で、ふと声が聞きたくなって。"}

        class Principals:
            def default(self):
                return "owner"

        class Proactive:
            def recent_texts(self, entity, limit=3):
                return []

        compose = make_heartbeat_composer(
            brain=Brain(), souls=None, persona_cores=None, activity_log=None, affect=None,
            prethinker=None, proactive=Proactive(), raw_log=None, principals=Principals(),
            speak_lang="ja", latest_window=lambda entity: "")
        text = compose("mira", {"label": "ふつう"}, due=(),
                       intent={"text": "観測表のこと考えてた。", "why": ""})
        self.assertEqual(text, "いま作業の合間で、ふと声が聞きたくなって。")
        self.assertIn("オーナーはこれを読んでいない", Brain.request.text)
        self.assertIn("観測表のこと考えてた。", Brain.request.text)

    def test_talk_digest(self) -> None:
        from agent.brain.compose.heartbeat_compose import talk_digest

        rows = [{"text": "昨日の表見たよ", "at": 1000.0 - 7200, "source": "discord:123"},
                {"text": "雨だね", "at": 1000.0 - 30, "source": "app:dm"}]
        out = talk_digest(rows, "観測表、見てくれる？", now=1000.0)
        self.assertIn("オーナー（2時間前・Discord）「昨日の表見たよ」", out)
        self.assertIn("オーナー（たったいま・アプリ）「雨だね」", out)
        self.assertIn("自分が最後に自分から送った一言「観測表、見てくれる？」", out)
        self.assertEqual(talk_digest([], "", now=1000.0), "")

    def test_make_recent_talk_reads_owner_lines_and_last_proactive(self) -> None:
        from agent.brain.compose.heartbeat_compose import make_recent_talk

        class Owner:
            id, owner = "owner", True

        class Principals:
            def all(self):
                return [Owner()]

        class Threads:
            asked = None

            def recent_owner_lines(self, entity, limit, *, owners):
                Threads.asked = (entity, limit, owners)
                return [{"text": "おはよう", "at": 990.0, "source": "app:dm"}]

        class Proactive:
            def recent_texts(self, entity, limit=3):
                return ["観測表できたよ"]

        recent = make_recent_talk(threads=Threads(), proactive=Proactive(),
                                  principals=Principals(), clock=lambda: 1000.0)
        out = recent("aster")
        self.assertEqual(Threads.asked, ("aster", 3, ("owner",)))
        self.assertIn("オーナー（たったいま・アプリ）「おはよう」", out)
        self.assertIn("観測表できたよ", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
