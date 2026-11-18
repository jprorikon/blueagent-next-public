#!/usr/bin/env python3
"""AppState 拆成窄服務（review.md 2026-10-25 P1）：每個服務不起 HTTP、不建 AppState 也能測。

判據：
  ① TurnService：跑一輪、等 job、YIELD 進 pending、遙測一輪一行、記「在哪個信道說話」、
     有人對她說話才記情緒（旁聽不算）；決策交給 on_decision；結果不含應用快照；
  ② StreamDelivery：先推 accepted、幀照順序、最後 result；音檔併進 outbox；send 炸了也照樣
     推 error 並寫 outbox；
  ③ VoiceTurns：work 的語音回合先短回（quick＋出聲）、正式答案排進佇列並帶 ack_text；
     跟短回覆幾乎一樣的後續不送；佇列拒收退回背景執行緒；
  ④ AppState.reset() 換了 App 之後，服務跟著重接（不會還拿著舊的 runtime）。
"""

from __future__ import annotations

import base64
import threading
import time
import types
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.followups import VoiceTurns
from agent.brain.compose.streaming import StreamDelivery
from agent.brain.compose.turns import TurnService, reply_text
from agent.brain.domain.principal import Principal, PrincipalRegistry


class _Decision:
    thread_id = "th_x-1"
    engage = "reply"
    deadline_ms = None

    def to_dict(self):
        return {"thread": self.thread_id, "pipeline": "chat.text", "principal": "owner"}


def _result(**overrides):
    base = dict(decision=_Decision(), job=None, yielded=False, yield_ms=None, sealed=None,
                observed=False, dropped=False, duplicate=False, reacted=False)
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _Runtime:
    def __init__(self, result, frames=()):
        self.result, self.frames, self.events = result, list(frames), []

    def handle(self, event, *, source):
        self.events.append(event)
        for frame in self.frames:
            source.emit_frame(frame) if hasattr(source, "emit_frame") else None
        return self.result


def _service(result, **deps):
    principals = PrincipalRegistry(default="owner")
    principals.register(Principal("owner", "主人", owner=True), replace=True)
    pending = mock.Mock()
    defaults = dict(runtime=_Runtime(result), threads=mock.Mock(), pending_store=pending,
                    lives={}, principals=principals)
    defaults.update(deps)
    return TurnService(**defaults), defaults


class TestTurnService(unittest.TestCase):
    def test_one_turn_with_side_effects(self):
        decisions, telemetry, delivery, affect = [], mock.Mock(), mock.Mock(), mock.Mock()
        affect.note_message.return_value = {"ok": True, "event_id": "m1"}
        service, deps = _service(_result(), telemetry=telemetry, delivery=delivery,
                                 affect=affect, on_decision=decisions.append)
        out = service.run("こんにちは", entity="mira", principal="owner")
        self.assertTrue(out["ok"], out)
        self.assertNotIn("state", out, "應用快照是 AppState 的事")
        self.assertEqual(out["room"], ["owner"])
        self.assertEqual(decisions[0]["engine"], "blueagent-next")
        telemetry.record.assert_called_once()
        delivery.note_seen.assert_called_once_with("owner", "app")
        affect.appraise.assert_called_once()
        deps["threads"].persist.assert_called_once()
        event = deps["runtime"].events[0]
        self.assertEqual((event.entity, event.principal, event.source), ("mira", "owner", "phone:owner"))

    def test_observed_turn_does_not_move_her_mood_and_yield_is_queued(self):
        affect = mock.Mock()
        service, deps = _service(_result(observed=True, yielded=True, yield_ms=1200), affect=affect)
        service.run("横で話してる", principal="owner", group=True, participants=["owner", "alice"])
        affect.note_message.assert_not_called()
        deps["pending_store"].add.assert_called_once()

    def test_yield_keeps_the_original_route_for_replay(self):
        """REVIEW 2026-11-08：重放要回到原回合的線與來源，不能落到預設的 Discord 大廳。"""
        service, deps = _service(_result(yielded=True, yield_ms=1200))
        service.run("それ、どう思う？", principal="owner", group=True,
                    participants=["owner", "alice"], source_override="api:command",
                    scope_override="group:room-x", scene_override="room-x")
        pending = deps["pending_store"].add.call_args.args[0]
        self.assertEqual((pending.source_override, pending.scope_override, pending.scene_override),
                         ("api:command", "group:room-x", "room-x"))
        from agent.brain.domain.pending import PendingIntervention
        again = PendingIntervention.from_dict(pending.to_dict())
        self.assertEqual(again.scope_override, "group:room-x", "落盤後重啟也要帶著路由")

    def test_yield_loop_replays_with_the_stored_route(self):
        import pathlib
        state = (pathlib.Path(__file__).resolve().parents[2] / "agent/brain/compose/state.py").read_text("utf-8")
        loop = state[state.index("def _yield_loop("):]
        loop = loop[:loop.index("def shutdown(")]
        for name in ("source_override", "scope_override", "scene_override"):
            self.assertIn(f"{name}=item.{name}", loop)

    def test_app_turn_yield_is_silence_not_a_replay(self):
        """App 的回覆只由 adapter 送回 App Server；重放沒有人送 ⇒ 不排重放（也就不會跑到 Discord）。"""
        service, deps = _service(_result(yielded=True, yield_ms=1200))
        service.run("みんな、どう？", principal="owner", group=True,
                    participants=["owner", "aster"], source_override="app:cnv_room",
                    scope_override="group:cnv_room", scene_override="app-group")
        deps["pending_store"].add.assert_not_called()

    def test_replay_skips_telemetry_and_empty_text_is_rejected(self):
        telemetry = mock.Mock()
        service, _ = _service(_result(), telemetry=telemetry)
        service.run("もう一回", replay=True)
        telemetry.record.assert_not_called()
        with self.assertRaises(ValueError):
            service.run("   ")

    def test_human_turn_pauses_only_that_entitys_life(self):
        life = mock.Mock()
        service, _ = _service(_result(), lives={"mira": life})
        service.run("ねえ", entity="mira")
        life.human_enter.assert_called_once()
        life.human_exit.assert_called_once()

    def test_wait_timeout_is_an_explicit_failure(self):
        job = mock.Mock()
        job.error = None
        job.wait.return_value = False
        service, _ = _service(_result(job=job))
        out = service.run("まだ考えてる", entity="mira", principal="owner")
        self.assertFalse(out["ok"], out)
        self.assertFalse(out["finished"], out)
        self.assertIn("TimeoutError", out["error"])


class TestStreamDelivery(unittest.TestCase):
    def _send(self, *, fail=False):
        clip = base64.b64encode(b"RIFFfake").decode()

        def send(**kwargs):
            on_frame = kwargs["on_frame"]
            on_frame({"kind": "delta", "payload": {"text": "こん"}})
            on_frame({"kind": "speech", "payload": {"audio": clip}})
            if fail:
                raise RuntimeError("boom")
            return {"ok": True, "frames": [{"kind": "done", "payload": {"text": "こんにちは"}}],
                    "decision": {"thread_id": "th", "frames": [1]}}

        return send

    def test_frames_in_order_then_result_and_outbox(self):
        outbox = mock.Mock()
        rows = list(StreamDelivery(send=self._send(), outbox=outbox).stream(text="やあ", principal="owner"))
        kinds = [(kind, (payload or {}).get("kind")) for kind, payload in rows]
        self.assertEqual(kinds, [("frame", "accepted"), ("frame", "delta"), ("frame", "speech"),
                                 ("result", None)])
        result = rows[-1][1]
        self.assertEqual(result["frames"], [])
        self.assertEqual(result["decision"]["frames"], [])
        recorded = outbox.record.call_args.kwargs
        self.assertEqual((recorded["text"], recorded["speech"], recorded["frames"]),
                         ("こんにちは", 1, 2))
        self.assertEqual(recorded["turn_id"], result["turn_id"])

    def test_error_still_writes_outbox(self):
        outbox = mock.Mock()
        rows = list(StreamDelivery(send=self._send(fail=True), outbox=outbox).stream(text="x"))
        self.assertEqual(rows[-1][0], "error")
        outbox.record.assert_called_once()

    def test_unfinished_result_does_not_enter_outbox(self):
        outbox = mock.Mock()

        def send(**_kwargs):
            return {"ok": False, "finished": False, "error": "TimeoutError: pending",
                    "frames": [], "decision": {}}

        rows = list(StreamDelivery(send=send, outbox=outbox).stream(text="x"))
        self.assertEqual(rows[-1][0], "result")
        self.assertFalse(rows[-1][1]["finished"])
        outbox.record.assert_not_called()


class TestVoiceTurns(unittest.TestCase):
    def _turns(self, *, submit_ok=True, followup_text="調べたよ：三件あった"):
        calls = []

        def send(text, **kwargs):
            calls.append(kwargs)
            body = "ちょっと待ってね" if kwargs.get("quick") else followup_text
            return {"ok": True, "frames": [{"kind": "done", "payload": {"text": body}}]}

        commands = mock.Mock()
        commands.submit.return_value = {"ok": submit_ok, "id": "c1", "reason": "full"}
        return VoiceTurns(send=send, commands=commands, bus=mock.Mock()), calls, commands

    def test_work_turn_acks_first_then_queues(self):
        turns, calls, commands = self._turns()
        ack = turns.send_voice("調べて", mode="work", ack_first=True, want_voice=True)
        self.assertTrue(calls[0]["quick"] and calls[0]["want_voice"])
        self.assertEqual(calls[0]["mode"], "chat")
        self.assertTrue(ack["following_up"])
        self.assertEqual(ack["command_id"], "c1")
        meta = commands.submit.call_args.kwargs["meta"]
        self.assertEqual(meta["ack_text"], "ちょっと待ってね")
        self.assertTrue(meta["want_voice"], "完整答案也要出聲")

    def test_followup_that_repeats_the_ack_is_dropped(self):
        turns, _, commands = self._turns()
        delivered = []
        turns.send_voice("調べて", mode="work", ack_first=True, on_followup=delivered.append)
        on_done = commands.submit.call_args.kwargs["on_done"]
        on_done({"result": {"frames": [{"kind": "done", "payload": {"text": "ちょっと待ってね"}}]}})
        self.assertEqual(delivered, [], "跟短回覆一樣的後續不送")
        on_done({"result": {"frames": [{"kind": "done", "payload": {"text": "三件あった"}}]}})
        self.assertEqual(len(delivered), 1)
        self.assertEqual(turns.bus.publish.call_count, 2, "App 的推送照樣兩次都推")

    def test_queue_rejection_falls_back_to_a_thread(self):
        turns, calls, _ = self._turns(submit_ok=False)
        done = threading.Event()
        turns.send_voice("調べて", mode="work", ack_first=True, on_followup=lambda _r: done.set())
        self.assertTrue(done.wait(2.0))
        self.assertTrue(any(call.get("followup") for call in calls))

    def test_chat_turn_goes_straight_through(self):
        turns, calls, commands = self._turns()
        turns.send_voice("やあ", mode="chat")
        self.assertEqual(len(calls), 1)
        commands.submit.assert_not_called()


class TestAppStateRewires(unittest.TestCase):
    def test_reset_rewires_services(self):
        state = make_state()
        self.addCleanup(state.shutdown)
        before = state.turns
        out = state.send("こんにちは", entity="mira", principal="owner")
        self.assertIn("state", out)
        self.assertEqual(reply_text(out), state.reply_text(out))
        with mock.patch.dict("os.environ", {"BLUEAGENT_ALLOW_RESET": "1"}):
            state.reset()
        self.assertIsNot(state.turns, before)
        self.assertIs(state.turns.runtime, state.app.runtime, "重接到新的 App")
        self.assertIs(state.streams.outbox, state.app.outbox)


if __name__ == "__main__":
    unittest.main(verbosity=2)
