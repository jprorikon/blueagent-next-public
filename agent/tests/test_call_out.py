#!/usr/bin/env python3
"""她打給主人（owner 2026-11-17）：撥號、結果、行動日誌措辭、自主打的頻率閘、`CallWish`。"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.adapter import app_call_out, app_voice
from agent.adapter.app_call_out import ANSWERED_NOTE, AppCallDialer
from agent.brain.runtime.inner.monologue import _parse_reply
from agent.brain.runtime.voice.outbound import CallWish, OutboundPolicy, self_call_allowed


class FakeClient:
    """App Server 的替身：`/state` 依序回 `states`（最後一個一直重複）。"""

    def __init__(self, states=("ringing", "declined"), *, error=None, reason="") -> None:
        self.states = list(states)
        self.error = error
        self.reason = reason
        self.calls = []

    def _request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/v1/calls/outgoing":
            if self.error:
                raise RuntimeError("app_server_http_" + self.error)
            return {"session": {"id": "call_out_1", "state": "ringing"}, "state": "ringing"}
        if path.endswith("/state"):
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"state": state, "reason": self.reason}
        return {"ok": True}


class Log:
    def __init__(self) -> None:
        self.rows = []

    def append(self, entity, **kw):
        self.rows.append({"entity": entity, **kw})


class Gate:
    def __init__(self, allowed=(True, "ok"), sent=True) -> None:
        self.allowed = allowed
        self.marked = []
        self.sent = []
        self.result = {"sent": sent, "reason": "ok" if sent else "quiet_hours"}

    def allows(self, entity, *, now=None, principal="owner"):
        return self.allowed

    def mark(self, entity, *, now=None, principal="owner"):
        self.marked.append((entity, principal))

    def maybe_send(self, entity, text, **kw):
        self.sent.append((entity, text, kw))
        return self.result


def make_app(*, gate=None, active=None):
    voice = SimpleNamespace(active=lambda entity: active, dialer=None)
    return SimpleNamespace(voice=voice, activity_log=Log(), proactive=gate,
                           delivery=SimpleNamespace(send=lambda *a, **k: None))


def dialer(app, client, **kw):
    return AppCallDialer(app, {"mira": client}, policy=kw.pop("policy", None)
                         or OutboundPolicy(daily_max=-1, gap_s=0), log=lambda _m: None,
                         poll_s=0.01, watch_s=kw.pop("watch_s", 2.0), **kw)


class DialOutcomeCase(unittest.TestCase):
    def run_call(self, client, *, gate=None, if_unanswered="", origin="chat"):
        app = make_app(gate=gate or Gate())
        dial = dialer(app, client)
        reply = dial.dial("mira", principal="owner", reason="声が聞きたい",
                          if_unanswered=if_unanswered, origin=origin)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual((reply["state"], reply["call_id"]), ("ringing", "call_out_1"))
        self.assertEqual(reply["frames"][0]["payload"]["state"], "ringing")
        dial.join("call_out_1")
        return app, dial

    def test_ringing_posts_to_the_owner_account_with_her_reason(self):
        client = FakeClient(("ringing", "live"))
        app, dial = self.run_call(client)
        method, path, body = client.calls[0]
        self.assertEqual((method, path, body), ("POST", "/v1/calls/outgoing",
                                                {"to": "owner", "topic": "声が聞きたい"}))
        self.assertEqual(app.activity_log.rows, [])           # 接了：不記「沒接」
        self.assertIsNone(dial.pending("call_out_1"))

    def test_declined_logs_the_unanswered_call_without_a_message_by_default(self):
        app, _ = self.run_call(FakeClient(("ringing", "declined")))
        row = app.activity_log.rows[-1]
        self.assertEqual(row["kind"], "call_unanswered")
        self.assertEqual(row["text"], "オーナーに電話をかけたけど、出てもらえなかった（切られた）")
        self.assertEqual(row["extra"]["outcome"], "declined")
        self.assertNotIn("delivered", row["extra"])
        self.assertEqual(app.proactive.sent, [])               # 沒說要補一句 ⇒ 不發私聊

    def test_timeout_is_logged_as_no_answer(self):
        app, _ = self.run_call(FakeClient(("ringing", "missed"), reason="timeout"))
        row = app.activity_log.rows[-1]
        self.assertIn("出なかった", row["text"])
        self.assertEqual((row["extra"]["outcome"], row["extra"]["reason"]), ("missed", "timeout"))

    def test_failed_accept_is_logged_as_not_connected(self):
        app, _ = self.run_call(FakeClient(("ringing", "connecting", "failed"), reason="busy_elsewhere"))
        row = app.activity_log.rows[-1]
        self.assertIn("つながらなかった", row["text"])
        self.assertEqual(row["extra"]["outcome"], "failed")

    def test_her_own_cancel_is_logged_as_cancelled_without_a_message(self):
        """審核 OC-4：她響鈴中自己掛掉 ⇒ outcome=cancelled、文案是「自分で切った」，就算有留言也不送。"""
        app, _ = self.run_call(FakeClient(("ringing", "cancelled"), reason="caller_cancelled"),
                               if_unanswered="また後でかけるね")
        row = app.activity_log.rows[-1]
        self.assertEqual(row["extra"]["outcome"], "cancelled")
        self.assertIn("自分で切った", row["text"])
        self.assertNotIn("つながらなかった", row["text"])
        self.assertNotIn("delivered", row["extra"])
        self.assertEqual(app.proactive.sent, [])

    def test_unknown_terminal_state_keeps_the_raw_state(self):
        app, _ = self.run_call(FakeClient(("ringing", "exploded")))
        row = app.activity_log.rows[-1]
        self.assertEqual((row["extra"]["outcome"], row["extra"]["state"]), ("unknown", "exploded"))
        self.assertNotIn("つながらなかった", row["text"])

    def test_her_follow_up_line_is_logged_only_after_delivery(self):
        """審核 R3 同一條：補的那一句送到了才寫「と送った」，沒送到寫「届かなかった」＋原因。"""
        app, _ = self.run_call(FakeClient(("declined",)), if_unanswered="また後でかけるね")
        row = app.activity_log.rows[-1]
        self.assertTrue(row["text"].endswith("：「また後でかけるね」と送った"), row["text"])
        self.assertTrue(row["extra"]["delivered"])
        entity, text, options = app.proactive.sent[0]
        self.assertEqual((entity, text, options["force"]), ("mira", "また後でかけるね", True))
        app, _ = self.run_call(FakeClient(("missed",)), gate=Gate(sent=False),
                               if_unanswered="また後でかけるね")
        row = app.activity_log.rows[-1]
        self.assertNotIn("と送った", row["text"])
        self.assertIn("届かなかった", row["text"])
        self.assertEqual((row["extra"]["delivered"], row["extra"]["failure"]), (False, "quiet_hours"))

    def test_watch_gives_up_hangs_up_and_logs_no_answer(self):
        client = FakeClient(("ringing",))
        app = make_app(gate=Gate())
        dial = dialer(app, client, watch_s=0.1)
        dial.dial("mira", principal="owner")
        dial.join("call_out_1")
        self.assertIn(("POST", "/v1/calls/call_out_1/hangup", {}), client.calls)
        self.assertEqual(app.activity_log.rows[-1]["extra"]["reason"], "watch_timeout")


class DialGuardCase(unittest.TestCase):
    def test_owner_already_on_a_call(self):
        app = make_app()
        reply = dialer(app, FakeClient(error="busy_elsewhere")).dial("mira", principal="owner")
        self.assertEqual((reply["ok"], reply["error"]), (False, "owner_busy"))
        self.assertEqual(reply["frames"][0]["payload"]["state"], "failed")

    def test_she_is_on_another_call(self):
        client = FakeClient()
        reply = dialer(make_app(active=object()), client).dial("mira", principal="owner")
        self.assertEqual(reply["error"], "busy_self")
        self.assertEqual(client.calls, [])

    def test_only_one_ringing_call_per_entity(self):
        client = FakeClient(("ringing",))
        dial = dialer(make_app(), client, watch_s=0.3)
        first = dial.dial("mira", principal="owner")
        second = dial.dial("mira", principal="owner")
        self.assertEqual((second["existing"], second["call_id"]), (True, first["call_id"]))
        self.assertEqual(sum(1 for call in client.calls if call[1] == "/v1/calls/outgoing"), 1)
        dial.join(first["call_id"])

    def test_no_app_account_for_her(self):
        reply = AppCallDialer(make_app(), {}, log=lambda _m: None).dial("aster", principal="owner")
        self.assertEqual(reply["error"], "end_unavailable:phone")


class SelfCallLimitCase(unittest.TestCase):
    def test_gate_reasons_block_her_own_calls(self):
        for reason in ("quiet_hours", "circadian_sleep", "owner_in_call", "daily_max"):
            with self.subTest(reason=reason):
                app = make_app(gate=Gate(allowed=(False, reason)))
                client = FakeClient()
                reply = dialer(app, client).dial("mira", principal="owner", origin="self")
                self.assertEqual(reply["error"], reason)
                self.assertEqual(client.calls, [])

    def test_chat_calls_skip_the_gate_but_self_calls_count_against_it(self):
        gate = Gate(allowed=(False, "quiet_hours"))
        app = make_app(gate=gate)
        dial = dialer(app, FakeClient(("live",)))
        self.assertTrue(dial.dial("mira", principal="owner", origin="chat")["ok"])
        dial.join("call_out_1")
        self.assertEqual(gate.marked, [])
        gate.allowed = (True, "ok")
        self.assertTrue(dial.dial("mira", principal="owner", origin="self")["ok"])
        dial.join("call_out_1")
        self.assertEqual(gate.marked, [("mira", "owner")])  # 算進主動消息的每日上限

    def test_call_daily_cap_and_gap(self):
        now = [time.mktime((2026, 11, 17, 10, 0, 0, 0, 0, -1))]
        policy = OutboundPolicy(daily_max=2, gap_s=3600, clock=lambda: now[0])
        self.assertEqual(policy.allows("mira"), (True, "ok"))
        policy.mark("mira")
        now[0] += 600
        self.assertEqual(policy.allows("mira"), (False, "call_gap"))
        now[0] += 3600
        policy.mark("mira")
        now[0] += 7200
        self.assertEqual(policy.allows("mira"), (False, "call_daily_max"))
        self.assertEqual(policy.allows("aster"), (True, "ok"))   # 額度每個角色各自算
        now[0] += 24 * 3600
        self.assertEqual(policy.allows("mira"), (True, "ok"))

    def test_policy_survives_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            path = f"{root}/call_out.json"
            OutboundPolicy(daily_max=1, gap_s=0, state_path=path).mark("mira")
            again = OutboundPolicy(daily_max=1, gap_s=0, state_path=path)
            self.assertEqual(again.allows("mira"), (False, "call_daily_max"))

    def test_switches(self):
        policy = OutboundPolicy(daily_max=-1, gap_s=0)
        with mock.patch.dict("os.environ", {"BLUEAGENT_CALL_OUT_SELF": "0"}):
            self.assertEqual(self_call_allowed("mira", "owner", gate=None, policy=policy),
                             (False, "self_call_off"))


class CallWishCase(unittest.TestCase):
    def wish(self, action, result):
        inner = SimpleNamespace(beat=lambda entity, facts, at=None: {"action": action, "text": "声が聞きたい",
                                                                      "why": "寂しい"},
                                enabled=True)
        dialed = []

        def dial(entity, row):
            dialed.append((entity, row["why"]))
            return result

        return CallWish(inner, dial=dial), dialed

    def test_call_placed_silences_the_beat(self):
        wish, dialed = self.wish("call", {"ok": True, "call_id": "call_out_1"})
        row = wish.beat("mira", facts={})
        self.assertEqual((row["action"], row["call"]["call_id"]), ("none", "call_out_1"))
        self.assertEqual(dialed, [("mira", "寂しい")])
        self.assertTrue(wish.enabled)                          # 其他屬性照轉

    def test_call_blocked_falls_back_to_talking(self):
        wish, _ = self.wish("call", {"ok": False, "error": "quiet_hours"})
        row = wish.beat("mira", facts={})
        self.assertEqual((row["action"], row["call"]["error"]), ("talk", "quiet_hours"))

    def test_other_actions_pass_through(self):
        wish, dialed = self.wish("work", {"ok": True})
        self.assertEqual(wish.beat("mira", facts={})["action"], "work")
        self.assertEqual(dialed, [])

    def test_monologue_accepts_the_call_action(self):
        self.assertEqual(_parse_reply('{"text": "声が聞きたい", "action": "call"}')["action"], "call")
        self.assertEqual(_parse_reply('{"text": "x", "action": "fly"}')["action"], "none")

    def test_monologue_carries_the_unanswered_line_only_for_calls(self):
        """自主打的也能留言（owner 2026-11-17）：獨白寫了 `if_unanswered` 才帶；不是 call 的拍子一律清空。"""
        row = _parse_reply('{"text": "声が聞きたい", "action": "call", "if_unanswered": "また夜にかけるね"}')
        self.assertEqual(row["if_unanswered"], "また夜にかけるね")
        row = _parse_reply('{"text": "作業する", "action": "work", "if_unanswered": "まぎれこみ"}')
        self.assertEqual(row["if_unanswered"], "")
        self.assertEqual(_parse_reply('{"text": "声が聞きたい", "action": "call"}')["if_unanswered"], "")

    def test_self_dial_forwards_the_unanswered_line(self):
        from agent.adapter.app_call_out import self_dial
        dial = mock.Mock(return_value={"ok": True, "call_id": "call_out_1"})
        voice = SimpleNamespace(dialer=SimpleNamespace(dial=dial))
        self_dial(voice)("mira", {"why": "寂しい", "if_unanswered": "また夜にかけるね"})
        dial.assert_called_once_with("mira", principal="owner", reason="寂しい", origin="self",
                                     if_unanswered="また夜にかけるね")
        dial.reset_mock()
        self_dial(voice)("mira", {"why": "寂しい"})
        self.assertEqual(dial.call_args.kwargs["if_unanswered"], "")


class BrainSideCase(unittest.TestCase):
    """主人接了：App Server 叫 `answer` ⇒ 她這頭掛上通話，第一句知道是自己打的。"""

    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)

    def body(self, action, call_id="call_out_1"):
        return {"action": action, "call_id": call_id, "account_id": "acct_owner",
                "principal": "owner", "entity": "mira", "topic": "声が聞きたい"}

    def test_voice_call_tool_rings_the_app_instead_of_a_local_call(self):
        dial = mock.Mock(return_value={"ok": True, "state": "ringing", "call_id": "call_out_1",
                                       "frames": [{"kind": "call", "payload": {"state": "ringing"}}]})
        self.state.app.voice.dialer = SimpleNamespace(dial=dial)
        tool = self.state.app.tools.get("voice.call")
        from agent.brain.runtime.tools.base import ToolContext
        ctx = ToolContext(entity="mira", principal="owner", thread_id="t1", source="app",
                          voice=self.state.app.voice)
        result = tool.run(ctx, {"end": "phone", "reason": "会いたい", "if_unanswered": "またね"})
        self.assertTrue(result.ok)
        dial.assert_called_once_with("mira", principal="owner", reason="会いたい",
                                     if_unanswered="またね", origin="chat")
        self.assertIsNone(self.state.app.voice.active("mira"))   # 主人接之前沒有通話

    def test_without_a_dialer_phone_stays_the_old_local_call(self):
        reply = self.state.app.voice.ring("mira", principal="owner")
        self.assertTrue(reply["ok"])
        self.assertEqual(self.state.app.voice.active("mira").end, "phone")

    def test_answer_attaches_with_her_reason_in_the_first_turn(self):
        status, reply = app_voice.handle(self.state, self.body("answer"))
        self.assertEqual(status, 200, reply)
        session = self.state.app.voice.active("mira")
        self.assertEqual((session.id, session.meta["pickup"]), ("call_out_1", "outgoing"))
        self.assertEqual(session.meta["pickup_note"],
                         ANSWERED_NOTE.format(why="かけた理由：声が聞きたい"))
        status, reply = app_voice.handle(self.state, self.body("answer", "call_other"))
        self.assertEqual((status, reply["error"]), (409, "busy_elsewhere"))

    def test_attach_wires_the_dialer_onto_voice(self):
        adapters = [SimpleNamespace(entity="mira", client=FakeClient())]
        found = app_call_out.attach(self.state.app, adapters)
        self.assertIs(self.state.app.voice.dialer, found)
        self.assertIsNone(app_call_out.attach(self.state.app, []))


if __name__ == "__main__":
    unittest.main()
