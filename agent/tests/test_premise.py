#!/usr/bin/env python3
"""出口閘（docs/PLAN-common-ground-20261109.md §4，第 5、6 步）。

判據：
  ① 規則層：最近沒有可見的對話卻用「例の／さっきの／見た？」指 ⇒ 重寫一次；還懸空 ⇒ 不發。
     「あれ？」這種驚訝的感嘆不算。
  ② 評審層：只給主人看得到的東西；說看不懂 ⇒ 帶著缺的前提重寫；判不了（超時／亂回）⇒ 放行。
  ③ 心跳：被擋 ⇒ 這一跳不發、記 `premise`；`raw` 路（舊行為）不過閘。
"""

from __future__ import annotations

import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.inner.premise import PremiseCheck, dangling


class TestRules(unittest.TestCase):
    def test_dangling_words(self) -> None:
        self.assertEqual(dangling("例の表、見た？"), ["例の", "見た？"])
        self.assertEqual(dangling("あれ？雨が降ってきた"), [], "驚訝的「あれ？」不收")
        self.assertEqual(dangling("観測表を作ってたんだけど、一枚できたよ"), [])

    def test_fresh_conversation_allows_deixis(self) -> None:
        check = PremiseCheck(critic=None, enabled=True)
        result = check.review("さっきの話の続きだけど", fresh=True)
        self.assertEqual(result["verdict"], "pass")

    def test_rewrite_once_then_drop(self) -> None:
        check = PremiseCheck(critic=None, enabled=True)
        hints = []
        good = check.review("例の表、見た？", fresh=False,
                            redo=lambda hint: hints.append(hint) or "観測表を一枚作ったんだ、見てほしい")
        self.assertEqual(good["verdict"], "rewrote")
        self.assertEqual(good["text"], "観測表を一枚作ったんだ、見てほしい")
        self.assertIn("例の", hints[0])
        bad = check.review("例の表、見た？", fresh=False, redo=lambda hint: "さっきの表どうだった？")
        self.assertEqual((bad["verdict"], bad["text"]), ("dropped", ""), "重寫後還懸空 ⇒ 不發")
        none = check.review("例の表、見た？", fresh=False)
        self.assertEqual(none["text"], "", "不能重寫 ⇒ 不發")

    def test_repeat_of_unanswered_is_rewritten_or_dropped(self) -> None:
        check = PremiseCheck(critic=None, enabled=True)
        earlier = ["観測表できたよ、見てほしいな"]
        same = check.review("観測表できたから見てほしい", fresh=True, previous=earlier)
        self.assertEqual(same["verdict"], "dropped", "不能重寫 ⇒ 不發")
        fixed = check.review("観測表できたから見てほしい", fresh=True, previous=earlier,
                             redo=lambda hint: "さっき送ったのは急がなくていいよ。いま雨すごいね")
        self.assertEqual(fixed["verdict"], "rewrote")
        still = check.review("観測表できたから見てほしい", fresh=True, previous=earlier,
                             redo=lambda hint: "観測表見てほしいな")
        self.assertEqual(still["text"], "", "重寫後還是同一件事 ⇒ 不發")
        other = check.review("雨が降ってきたね", fresh=True, previous=earlier)
        self.assertEqual(other["verdict"], "pass")

    def test_critic_sees_previous(self) -> None:
        prompts = []
        check = PremiseCheck(critic=lambda prompt: prompts.append(prompt) or '{"ok": true}',
                             enabled=True, critic_enabled=True)
        check.review("雨が降ってきたね", previous=["観測表できたよ"])
        self.assertIn("前に送って、まだ返事のない一言：「観測表できたよ」", prompts[0])

    def test_switch_off(self) -> None:
        check = PremiseCheck(critic=None, enabled=False)
        self.assertEqual(check.review("例の表、見た？")["text"], "例の表、見た？")


class TestCritic(unittest.TestCase):
    def test_critic_sees_only_visible_and_triggers_rewrite(self) -> None:
        prompts = []

        def critic(prompt):
            prompts.append(prompt)
            return '{"ok": false, "missing": "何の表か分からない"}'

        check = PremiseCheck(critic=critic, enabled=True, critic_enabled=True)
        result = check.review("表、一枚できた", visible=["オーナー（3時間前）：おやすみ"],
                              redo=lambda hint: f"観測表を作ってた。{hint}")
        self.assertEqual(result["verdict"], "rewrote")
        self.assertIn("何の表か分からない", result["text"])
        self.assertIn("オーナー（3時間前）：おやすみ", prompts[0])
        self.assertIn("表、一枚できた", prompts[0])

    def test_critic_ok_passes(self) -> None:
        check = PremiseCheck(critic=lambda prompt: '{"ok": true, "missing": ""}',
                             enabled=True, critic_enabled=True)
        self.assertEqual(check.review("雨が降ってきたね")["verdict"], "pass")

    def test_critic_failures_let_it_through(self) -> None:
        for critic in (lambda prompt: "うーん", lambda prompt: (_ for _ in ()).throw(RuntimeError())):
            check = PremiseCheck(critic=critic, enabled=True, critic_enabled=True)
            self.assertEqual(check.review("雨が降ってきたね")["verdict"], "pass")
        slow = PremiseCheck(critic=lambda prompt: time.sleep(1) or '{"ok": false}',
                            enabled=True, critic_enabled=True, timeout_s=0.05)
        self.assertEqual(slow.review("雨が降ってきたね")["verdict"], "pass", "超時 ⇒ 放行")
        self.assertEqual(slow.stats["critic_errors"], 1)

    def test_critic_switch(self) -> None:
        called = []
        check = PremiseCheck(critic=lambda prompt: called.append(1) or '{"ok": false}',
                             enabled=True, critic_enabled=False)
        self.assertEqual(check.review("雨が降ってきたね")["verdict"], "pass")
        self.assertEqual(called, [])


class TestHeartbeatHook(unittest.TestCase):
    def beat(self, premise, *, talk_route="compose", monologue=None, compose=None):
        from agent.brain.runtime.heartbeat import Heartbeat
        from test_monologue import StubAffect, StubGate, StubMonologue

        gate = StubGate()
        beat = Heartbeat(entity="mira", affect=StubAffect(), gate=gate, publish=None,
                         threshold=0.35, clock=lambda: 1000.0, talk_route=talk_route,
                         monologue=monologue or StubMonologue("none"),
                         compose=compose or (lambda entity, mood, due=(), intent=None:
                                             "例の表、見た？"),
                         premise=premise,
                         visible=lambda entity: {"lines": [], "fresh": False})
        beat.arousal_pacing = False
        beat.tick()
        return beat, gate

    def test_dangling_draft_is_not_sent(self) -> None:
        beat, gate = self.beat(PremiseCheck(critic=None, enabled=True))
        self.assertEqual(gate.sent, [])
        self.assertEqual(beat.last_skip_reason, "premise")
        self.assertEqual(beat.snapshot()["last_premise"]["verdict"], "dropped")

    def test_rewrite_gets_the_hint(self) -> None:
        seen = []

        def compose(entity, mood, due=(), intent=None):
            seen.append(intent)
            return "観測表を作ってたんだ、一枚できたよ" if intent and intent.get("fix") else "例の表、見た？"

        _beat, gate = self.beat(PremiseCheck(critic=None, enabled=True), compose=compose)
        self.assertEqual(gate.sent, ["観測表を作ってたんだ、一枚できたよ"])
        self.assertIn("例の", seen[-1]["fix"])

    def test_raw_route_skips_the_gate(self) -> None:
        from test_monologue import StubMonologue

        _beat, gate = self.beat(PremiseCheck(critic=None, enabled=True), talk_route="raw",
                                monologue=StubMonologue("talk", text="例の表、見た？"))
        self.assertEqual(gate.sent, ["例の表、見た？"], "raw＝舊行為")


class TestVisibleAndOpener(unittest.TestCase):
    def test_make_visible_reads_only_the_private_line(self) -> None:
        from types import SimpleNamespace as NS

        from agent.brain.compose.heartbeat_compose import make_visible

        def thread(scope, people, turns):
            return NS(key=NS(entity="mira", scope=scope), participants=set(people),
                      transcript=[NS(role=role, text=text, at=at) for role, text, at in turns])

        threads = NS(threads=lambda: [
            thread("owner", {"owner", "mira"}, [("user", "雨だね", 900.0),
                                                   ("assistant", "ほんとだ", 950.0)]),
            thread("group:c1", {"owner", "mira", "aster"}, [("user", "群の話", 990.0)]),
            thread("alice", {"alice", "mira"}, [("user", "客の話", 995.0)]),
        ])
        view = make_visible(threads, clock=lambda: 1000.0)("mira")
        self.assertEqual(len(view["lines"]), 2)
        self.assertIn("オーナー（1分前）：雨だね", view["lines"][0])
        self.assertTrue(view["fresh"])
        stale = make_visible(threads, clock=lambda: 1000.0 + 7200)("mira")
        self.assertFalse(stale["fresh"])

    def test_intent_fix_line(self) -> None:
        from agent.brain.compose.heartbeat_compose import intent_lines

        lines = intent_lines({"fix": "何の表か見えていない"})
        self.assertEqual(len(lines), 1)
        self.assertIn("通じなかった", lines[0])

    def test_group_opener_drops_dangling(self) -> None:
        from agent.brain.compose.group_opener import GroupOpener

        opener = GroupOpener(brain=None, threads=None)
        self.assertEqual(opener._grounded("aster", "例の話どうなった？", [], 3600.0), "")
        self.assertEqual(opener._grounded("aster", "例の話どうなった？", [("nori", "x")], 600.0),
                         "例の話どうなった？", "房裡剛有人說話 ⇒ 指得到")
        self.assertEqual(opener._grounded("aster", "今日は雨だね", [], 3600.0), "今日は雨だね")


if __name__ == "__main__":
    unittest.main(verbosity=2)
