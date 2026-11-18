#!/usr/bin/env python3
"""App 來電「接不接」（owner 2026-11-17）：規則、模型判斷的時間上限、`ring` 動作與拒接收尾。"""

from __future__ import annotations

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from demo_common import *  # noqa: F401,F403
from test_app_voice_bridge import FakeSTT, payload, utterance

from agent.adapter import app_call_decline, app_voice
from agent.brain.runtime.inner.call_pickup import FALLBACK_LINE, WOKEN_NOTE, CallPickup, parse_verdict


class FakeClock:
    def __init__(self, state="awake") -> None:
        self.value = state

    def state(self, _now=None):
        return self.value

    def asleep(self, _now=None):
        return self.value == "asleep"


class FakeVoice:
    def __init__(self, active=None) -> None:
        self.session = active

    def active(self, _entity):
        return self.session


class FakeLoop:
    def __init__(self, status="") -> None:
        self.status = status

    def snapshot(self):
        return {"activity": {"status": self.status, "title": "レポートの整理"} if self.status else None}


class FakeScheduler:
    def __init__(self, lanes=None) -> None:
        self.lanes = lanes or {}

    def snapshot(self):
        return {"active_by_entity": {"mira": self.lanes} if self.lanes else {}}


def fake_app(*, clock="awake", active=None, activity="", lanes=None):
    return SimpleNamespace(circadian={"mira": FakeClock(clock)}, voice=FakeVoice(active),
                           lives={"mira": FakeLoop(activity)}, scheduler=FakeScheduler(lanes),
                           entities=None, affect=None)


class Clock:
    def __init__(self, at=1_000_000.0) -> None:
        self.at = at

    def __call__(self):
        return self.at


class CallPickupRulesCase(unittest.TestCase):
    def test_free_answers_immediately_without_asking_the_model(self):
        judge = mock.Mock(side_effect=AssertionError("閒著不該叫模型"))
        result = CallPickup(judge=judge).decide(fake_app(), "mira", "owner", "call_1")
        self.assertEqual((result.answer, result.reason), (True, "free"))
        judge.assert_not_called()

    def test_already_on_another_call_is_line_busy(self):
        other = SimpleNamespace(id="call_discord")
        result = CallPickup().decide(fake_app(active=other), "mira", "owner", "call_1")
        self.assertEqual((result.answer, result.reason), (False, "on_call"))
        same = SimpleNamespace(id="call_1")
        self.assertTrue(CallPickup().decide(fake_app(active=same), "mira", "owner", "call_1").answer)

    def test_asleep_rings_out_then_second_call_wakes_her(self):
        clock = Clock()
        picker = CallPickup(clock=clock)
        app = fake_app(clock="asleep")
        first = picker.decide(app, "mira", "owner", "call_1")
        self.assertEqual((first.answer, first.reason, first.line), (False, "asleep", ""))
        clock.at += 120
        second = picker.decide(app, "mira", "owner", "call_2")
        self.assertEqual((second.answer, second.reason, second.note), (True, "woken", WOKEN_NOTE))

    def test_wake_window_expires(self):
        clock = Clock()
        picker = CallPickup(clock=clock)
        app = fake_app(clock="asleep")
        picker.decide(app, "mira", "owner", "call_1")
        clock.at += picker.wake_window_s + 1
        self.assertEqual(picker.decide(app, "mira", "owner", "call_2").reason, "asleep")
        # 別的人打來不算同一串
        self.assertEqual(picker.decide(app, "mira", "guest", "call_3").reason, "asleep")

    def test_busy_without_model_still_answers(self):
        result = CallPickup().decide(fake_app(activity="running", lanes={"interactive": 1}),
                                     "mira", "owner", "call_1")
        self.assertEqual((result.answer, result.reason), (True, "busy_but_answered"))
        self.assertIn("mid_turn", result.signals)
        self.assertTrue(any(signal.startswith("activity:") for signal in result.signals))

    def test_realtime_lane_alone_is_not_busy(self):
        result = CallPickup().decide(fake_app(lanes={"realtime": 1}), "mira", "owner", "c")
        self.assertEqual(result.reason, "free")

    def test_model_declines_with_her_line(self):
        prompts = []

        def judge(prompt):
            prompts.append(prompt)
            return '考えた。{"answer": false, "line": "いまレポート中、終わったらかけ直すね"}'

        result = CallPickup(judge=judge).decide(fake_app(clock="winding", activity="running"),
                                                "mira", "owner", "call_1")
        self.assertEqual((result.answer, result.reason), (False, "declined"))
        self.assertEqual(result.line, "いまレポート中、終わったらかけ直すね")
        self.assertIn("もうすぐ寝る時間", prompts[0])
        self.assertIn("レポートの整理", prompts[0])

    def test_model_decline_without_line_gets_fallback(self):
        result = CallPickup(judge=lambda _p: '{"answer": false}').decide(
            fake_app(activity="running"), "mira", "owner", "c")
        self.assertEqual(result.line, FALLBACK_LINE)

    def test_slow_or_broken_model_answers_within_the_time_limit(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.dict(os.environ, {"BLUEAGENT_CALL_JUDGE_TIMEOUT_S": "0.2"}):
            picker = CallPickup(judge=lambda _p: release.wait(5) and '{"answer": false}')
        started = time.monotonic()
        result = picker.decide(fake_app(activity="running"), "mira", "owner", "c")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual((result.answer, result.reason), (True, "judge_timeout"))
        broken = CallPickup(judge=lambda _p: "うーん、どうしよう").decide(
            fake_app(activity="running"), "mira", "owner", "c")
        self.assertEqual((broken.answer, broken.reason), (True, "judge_unparsable"))

        def boom(_prompt):
            raise RuntimeError("down")
        failed = CallPickup(judge=boom).decide(fake_app(activity="running"), "mira", "owner", "c")
        self.assertEqual((failed.answer, failed.reason), (True, "judge_failed"))

    def test_switches(self):
        with mock.patch.dict(os.environ, {"BLUEAGENT_CALL_PICKUP": "0"}):
            self.assertEqual(CallPickup().decide(fake_app(clock="asleep"), "mira", "o", "c").reason,
                             "always")
        judge = mock.Mock(return_value='{"answer": false}')
        with mock.patch.dict(os.environ, {"BLUEAGENT_CALL_PICKUP_JUDGE": "0"}):
            result = CallPickup(judge=judge).decide(fake_app(activity="running"), "mira", "o", "c")
        self.assertTrue(result.answer)
        judge.assert_not_called()

    def test_parse_verdict(self):
        self.assertEqual(parse_verdict('{"answer": true}'), (True, ""))
        self.assertIsNone(parse_verdict('{"answer": "no"}'))
        self.assertIsNone(parse_verdict(""))
        self.assertEqual(parse_verdict('{"answer": false, "line": "a\\n b"}'), (False, "a b"))


class RingActionCase(unittest.TestCase):
    """`app_voice` 的 `ring`：接了才掛上通話；不接回理由、背景收尾。"""

    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.state.app.stt = FakeSTT()
        self.clock = FakeClock("awake")
        self.state.app.circadian = {"mira": self.clock}
        self.judge = mock.Mock(return_value='{"answer": true}')
        self.state.app.call_pickup = CallPickup(judge=self.judge)

    def ring(self, call_id="call_platform_1"):
        status, reply = app_voice.handle(self.state, {**payload(call_id), "action": "ring"})
        self.assertEqual(status, 200, reply)
        return reply

    def test_answer_attaches_the_platform_call(self):
        reply = self.ring()
        self.assertTrue(reply["answer"])
        self.assertIn(reply["reason"], {"free", "judged_yes"})   # 示範基座的生活環可能正在跑活動
        self.assertEqual(reply["session"]["id"], "call_platform_1")
        self.assertIsNotNone(self.state.app.voice.active("mira"))
        again = self.ring()                       # 重送同一通：冪等
        self.assertEqual(again["reason"], "existing")

    def test_asleep_declines_without_attaching_and_logs_the_missed_call(self):
        self.clock.value = "asleep"
        with mock.patch.object(app_call_decline, "after",
                               side_effect=lambda *a, **k: app_call_decline._finish(
                                   a[0].app, a[1], a[2], a[3])):
            reply = self.ring()
        self.assertEqual((reply["answer"], reply["reason"], reply["line"]), (False, "asleep", ""))
        self.assertIsNone(self.state.app.voice.active("mira"))
        rows = self.state.app.activity_log.entries("mira", limit=5)
        self.assertEqual(rows[-1]["kind"], "call_missed")
        self.assertIn("寝ている間", rows[-1]["text"])

    def test_woken_call_passes_the_sleepy_note_to_the_first_turn_only(self):
        self.clock.value = "asleep"
        self.assertFalse(self.ring("call_a")["answer"])
        woken = self.ring("call_platform_1")
        self.assertEqual((woken["answer"], woken["reason"]), (True, "woken"))
        seen = []
        original = self.state.send_stream

        def spy(**kwargs):
            seen.append(kwargs.get("room_note"))
            return original(**kwargs)

        self.state.send_stream = spy
        for _ in range(2):
            status, reply = app_voice.handle(self.state, utterance())
            self.assertEqual(status, 200, reply)
        self.assertEqual(seen, [WOKEN_NOTE, ""])

    def test_declined_line_goes_out_as_a_direct_message(self):
        self.judge.return_value = '{"answer": false, "line": "いま手が離せない、あとでね"}'
        loop = self.state.app.lives.get("mira")
        sent = []
        self.state.app.proactive = mock.Mock()
        self.state.app.proactive.maybe_send.side_effect = \
            lambda entity, text, **kw: sent.append((entity, text, kw)) or {"sent": True}
        with mock.patch.object(type(loop), "snapshot",
                               return_value={"activity": {"status": "running", "title": "読書"}}), \
                mock.patch.object(app_call_decline, "after",
                                  side_effect=lambda *a, **k: app_call_decline._finish(
                                      a[0].app, a[1], a[2], a[3])):
            reply = self.ring()
        self.assertEqual((reply["answer"], reply["reason"]), (False, "declined"))
        self.assertEqual(reply["line"], "いま手が離せない、あとでね")
        self.assertIsNone(self.state.app.voice.active("mira"))
        self.assertEqual(len(sent), 1)
        entity, text, options = sent[0]
        self.assertEqual((entity, text, options["principal"]), ("mira", "いま手が離せない、あとでね", "owner"))
        self.assertTrue(options["force"])
        self.assertIs(options["publish"].__self__, self.state.app.delivery)

    def test_decline_log_reflects_the_real_delivery(self):
        """審核 R3：行動日誌寫的是送出的結果——沒發送端／拋例外／sent=False／送到，四種措辭各不相同。"""
        from types import SimpleNamespace
        decision = SimpleNamespace(reason="declined", line="あとでね")

        class Log:
            def __init__(self):
                self.rows = []

            def append(self, entity, **kw):
                self.rows.append(kw)

        def run(**app_fields):
            log = Log()
            app = SimpleNamespace(activity_log=log, proactive=None, delivery=None)
            for key, value in app_fields.items():
                setattr(app, key, value)
            app_call_decline._finish(app, "mira", "owner", decision)
            self.assertEqual(len(log.rows), 1)
            return log.rows[0]

        delivery = SimpleNamespace(send=lambda *a, **k: None)
        gate = lambda **behaviour: SimpleNamespace(maybe_send=mock.Mock(**behaviour))
        cases = {
            "no_channel": run(),
            "error": run(proactive=gate(side_effect=RuntimeError("down")), delivery=delivery),
            "not_sent": run(proactive=gate(return_value={"sent": False, "reason": "quiet_hours"}),
                            delivery=delivery),
            "sent": run(proactive=gate(return_value={"sent": True}), delivery=delivery),
        }
        for name in ("no_channel", "error", "not_sent"):
            with self.subTest(case=name):
                row = cases[name]
                self.assertNotIn("と送った", row["text"])
                self.assertIn("届かなかった", row["text"])
                self.assertFalse(row["extra"]["delivered"])
        self.assertEqual(cases["no_channel"]["extra"]["failure"], "no_channel")
        self.assertEqual(cases["error"]["extra"]["failure"], "error:RuntimeError")
        self.assertEqual(cases["not_sent"]["extra"]["failure"], "quiet_hours")
        self.assertIn("「あとでね」と送った", cases["sent"]["text"])
        self.assertTrue(cases["sent"]["extra"]["delivered"])

    def test_on_another_call_is_reported_as_line_busy(self):
        original = self.state.app.voice.call("mira", end="phone", principal="owner")
        self.assertTrue(original["ok"])
        reply = self.ring()
        self.assertEqual((reply["answer"], reply["reason"]), (False, "on_call"))


if __name__ == "__main__":
    unittest.main()
