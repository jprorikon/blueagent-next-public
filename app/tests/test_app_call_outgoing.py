#!/usr/bin/env python3
"""她打給主人（owner 2026-11-17）：響鈴、接聽、拒接、逾時、她自己掛掉、同時只能一通；沒接 ⇒ 私聊留「不在着信」。"""

from __future__ import annotations

import time
import unittest

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.social.models import BoardError
import test_app_server_voice as base                # 模組引入：不讓 loader 在這裡再跑一遍那邊的案例
from test_app_server_voice import FakeBrain, frame


class AnsweringBrain(FakeBrain):
    """多認得 `answer`；`refuse` 給一個錯誤碼 ⇒ 她那頭掛不上；`old` ⇒ 不認得 `answer` 的舊 brain。"""

    def __init__(self) -> None:
        super().__init__()
        self.refuse = ""
        self.old = False
        #: brain 其實掛上了、但回應在路上丟了（逾時／斷線）⇒ 平台只看到 503
        self.lost = False
        #: 收尾的 `hangup` 也失敗
        self.hangup_fails = False

    def call(self, action, payload):
        if action == "answer":
            with self.lock:
                self.calls.append((action, dict(payload)))
            if self.old:
                raise BoardError(400, "bad_voice_action")
            if self.lost:
                raise BoardError(503, "brain_voice_unavailable")
            if self.refuse:
                return {"ok": False, "error": self.refuse}
            return {"ok": True}
        if action == "hangup" and self.hangup_fails:
            with self.lock:
                self.calls.append((action, dict(payload)))
            raise BoardError(503, "brain_voice_unavailable")
        return super().call(action, payload)


class OutgoingCallCase(unittest.TestCase):
    # 借用主人撥號那邊的夾具（同一套帳號：owner 只看得到 mira）
    request, open_call, wait_state = (base.AppServerVoiceCase.request,
                                      base.AppServerVoiceCase.open_call,
                                      base.AppServerVoiceCase.wait_state)
    wait_events, speak = base.AppServerVoiceCase.wait_events, base.AppServerVoiceCase.speak

    def setUp(self):
        base.AppServerVoiceCase.setUp(self)
        self.brain = AnsweringBrain()
        self.server.services.voice.bridge = self.brain

    def dial(self, token="mira-token", to="owner", topic="声が聞きたくなった"):
        return self.request("POST", "/v1/calls/outgoing", {"to": to, "topic": topic},
                            token=token)

    def ringing(self):
        status, body = self.dial()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "ringing")
        self.assertEqual(body["session"]["caller"], "entity")
        self.assertGreater(body["expires_at"], time.time() + 20)   # 預設響 30 秒
        return body["session"]["id"]

    def owner_events(self, token="owner-token"):
        status, body = self.request("GET", "/v1/events", token=token)
        self.assertEqual(status, 200, body)
        return [event for event in body["events"] if event["type"].startswith("call.")]

    def test_ringing_is_pushed_only_to_the_owner(self):
        call_id = self.ringing()
        events = self.owner_events()
        self.assertEqual([event["type"] for event in events], ["call.incoming"])
        payload = events[0]["payload"]
        self.assertEqual((payload["call_id"], payload["entity"], payload["state"]),
                         (call_id, "mira", "ringing"))
        self.assertEqual(payload["ring_s"], 30.0)
        self.assertEqual(self.owner_events("aster-token"), [])       # 別的角色看不到
        self.assertEqual(self.owner_events("guest-token"), [])
        status, body = self.request("GET", "/v1/calls/incoming")
        self.assertEqual((status, body["session"]["id"]), (200, call_id))
        # 她自己也能問這一通的狀態（Agent 靠這一支寫結果）
        status, body = self.request("GET", f"/v1/calls/{call_id}/state", token="mira-token")
        self.assertEqual((status, body["state"]), (200, "ringing"))
        status, _ = self.request("GET", f"/v1/calls/{call_id}/state", token="aster-token")
        self.assertEqual(status, 404)

    def test_accept_goes_live_and_reuses_frames_and_hangup(self):
        call_id = self.ringing()
        status, body = self.request("POST", f"/v1/calls/{call_id}/accept", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "live")
        self.assertEqual(body["audio"]["sample_rate"], 16000)
        answer = [payload for action, payload in self.brain.calls if action == "answer"]
        self.assertEqual(len(answer), 1)
        self.assertEqual((answer[0]["caller"], answer[0]["topic"], answer[0]["entity"]),
                         ("entity", "声が聞きたくなった", "mira"))
        self.assertEqual(self.request("GET", "/v1/calls/incoming")[1]["session"], None)
        # 現有的音訊幀／通話事件流照用
        self.speak(call_id)
        event, _ = self.wait_events(call_id, "speech")
        self.assertEqual(event["payload"]["text"], "おはよう")
        status, again = self.request("POST", f"/v1/calls/{call_id}/accept", {})
        self.assertEqual((status, again["error"]), (410, "call_ended"))
        status, body = self.request("POST", f"/v1/calls/{call_id}/hangup", {})
        self.assertEqual(status, 200, body)
        self.assertIn("hangup", [action for action, _ in self.brain.calls])
        states = [event["payload"]["state"] for event in self.owner_events()]
        self.assertEqual(states, ["ringing", "live"])

    def test_decline_ends_ringing_and_she_can_read_the_result(self):
        call_id = self.ringing()
        status, body = self.request("POST", f"/v1/calls/{call_id}/decline", {})
        self.assertEqual((status, body["state"]), (200, "declined"))
        status, body = self.request("GET", f"/v1/calls/{call_id}/state", token="mira-token")
        self.assertEqual((body["state"], body["reason"]), ("declined", "declined"))
        self.assertNotIn("answer", [action for action, _ in self.brain.calls])
        updated = self.owner_events()[-1]
        self.assertEqual((updated["type"], updated["payload"]["state"]),
                         ("call.updated", "declined"))
        status, body = self.request("POST", f"/v1/calls/{call_id}/accept", {})
        self.assertEqual((status, body["error"]), (410, "call_ended"))

    def test_owner_hangup_while_ringing_counts_as_decline(self):
        call_id = self.ringing()
        status, body = self.request("POST", f"/v1/calls/{call_id}/hangup", {})
        self.assertEqual((status, body["state"]), (200, "declined"))

    def test_unanswered_ring_times_out_as_missed(self):
        self.server.services.voice.out_ring_s = 0.3
        status, body = self.dial()
        self.assertEqual(status, 200, body)
        call_id = body["session"]["id"]
        body = self.wait_state(call_id, "missed")
        self.assertEqual(body["reason"], "timeout")
        status, body = self.request("POST", f"/v1/calls/{call_id}/accept", {})
        self.assertEqual((status, body["error"]), (410, "call_ended"))
        self.assertEqual(self.owner_events()[-1]["payload"]["state"], "missed")

    def test_she_can_cancel_her_own_ringing_call(self):
        call_id = self.ringing()
        status, body = self.request("POST", f"/v1/calls/{call_id}/hangup", {},
                                    token="mira-token")
        self.assertEqual((status, body["state"]), (200, "cancelled"))
        status, body = self.request("POST", f"/v1/calls/{call_id}/accept", {})
        self.assertEqual(status, 410)

    def call_records(self):
        """主人↔mira 私聊裡 `kind=call` 的那幾則。"""
        status, body = self.request("POST", "/v1/conversations/direct", {"account_id": "mira"})
        self.assertEqual(status, 200, body)
        conversation_id = (body.get("conversation") or body)["id"]
        status, body = self.request("GET", f"/v1/conversations/{conversation_id}/messages")
        self.assertEqual(status, 200, body)
        return [message for message in body["messages"]
                if any(part["kind"] == "call" for part in message["parts"])]

    def test_missed_and_cancelled_leave_a_record_in_the_dm(self):
        """沒接（逾時）、她響鈴中掛掉 ⇒ 私聊各留一則「不在着信」，作者是她；拒接、接了都不留。"""
        voice = self.server.services.voice
        voice.out_ring_s = 0.3
        status, body = self.dial()
        missed_id = body["session"]["id"]
        self.wait_state(missed_id, "missed")
        voice.out_ring_s = 30.0
        cancelled_id = self.ringing()
        self.request("POST", f"/v1/calls/{cancelled_id}/hangup", {}, token="mira-token")
        declined_id = self.ringing()
        self.request("POST", f"/v1/calls/{declined_id}/decline", {})
        deadline = time.time() + 3
        records = self.call_records()
        while len(records) < 2 and time.time() < deadline:
            time.sleep(0.05)
            records = self.call_records()
        self.assertEqual(len(records), 2, records)
        self.assertEqual({message["author"]["id"] for message in records}, {"mira"})
        for message in records:
            self.assertEqual([(part["kind"], part["text"]) for part in message["parts"]], [("call", "")])

    def test_clients_cannot_send_call_parts(self):
        status, body = self.request("POST", "/v1/conversations/direct", {"account_id": "mira"})
        conversation_id = (body.get("conversation") or body)["id"]
        status, body = self.request("POST", f"/v1/conversations/{conversation_id}/messages",
                                    {"parts": [{"kind": "call", "text": ""}]})
        self.assertEqual((status, body["error"]), (400, "invalid_part_kind"))

    def test_only_one_call_per_account(self):
        call_id = self.ringing()
        status, body = self.dial()                                # 同一個她再撥
        self.assertEqual((status, body["error"]), (409, "busy_elsewhere"))
        status, body = self.request("POST", "/v1/calls", {"entity": "mira"})
        self.assertEqual((status, body["error"]), (409, "busy_elsewhere"))   # 主人那頭也撥不出去
        self.request("POST", f"/v1/calls/{call_id}/decline", {})
        live = self.open_call()                                   # 主人打給她、通話中
        status, body = self.dial()
        self.assertEqual((status, body["error"]), (409, "busy_elsewhere"))
        self.request("POST", f"/v1/calls/{live}/hangup", {})
        self.assertEqual(self.dial()[0], 200)                    # 收線之後就能打

    def test_who_may_dial_whom(self):
        status, body = self.request("POST", "/v1/calls/outgoing", {"to": "owner"})
        self.assertEqual((status, body["error"]), (403, "forbidden"))     # 主人不走這支
        status, body = self.dial(token="aster-token")                      # 主人看不到 aster
        self.assertEqual((status, body["error"]), (403, "call_entity_forbidden"))
        status, body = self.dial(to="guest")
        self.assertEqual((status, body["error"]), (404, "owner_not_found"))
        call_id = self.ringing()
        status, _ = self.request("POST", f"/v1/calls/{call_id}/accept", {}, token="mira-token")
        self.assertEqual(status, 404)                                    # 她不能替主人接

    def test_brain_refusing_the_answer_fails_the_call(self):
        self.brain.refuse = "busy_elsewhere"
        call_id = self.ringing()
        status, body = self.request("POST", f"/v1/calls/{call_id}/accept", {})
        self.assertEqual((status, body["error"]), (409, "busy_elsewhere"))
        status, body = self.request("GET", f"/v1/calls/{call_id}/state", token="mira-token")
        self.assertEqual((body["state"], body["reason"]), ("failed", "busy_elsewhere"))
        status, body = self.request("POST", f"/v1/calls/{call_id}/frames",
                                    {"seq": 1, "audio": frame()})
        self.assertEqual(status, 410)

    def hangups(self):
        return [payload for action, payload in self.brain.calls if action == "hangup"]

    def test_lost_answer_reply_releases_the_brain_side(self):
        """審核 OC-3：brain 掛上了但回應丟了 ⇒ 平台轉 failed，並照同一個 call_id 叫 brain 收掉；之後能再撥。"""
        self.brain.lost = True
        call_id = self.ringing()
        status, body = self.request("POST", f"/v1/calls/{call_id}/accept", {})
        self.assertEqual((status, body["error"]), (503, "brain_voice_unavailable"))
        status, body = self.request("GET", f"/v1/calls/{call_id}/state", token="mira-token")
        self.assertEqual((body["state"], body["reason"]), ("failed", "brain_voice_unavailable"))
        self.assertEqual([payload["call_id"] for payload in self.hangups()], [call_id])
        self.assertEqual(self.owner_events()[-1]["payload"]["state"], "failed")
        self.brain.lost = False
        self.ringing()                                         # 沒有殘留狀態擋住下一通

    def test_failed_release_keeps_the_original_error(self):
        self.brain.lost, self.brain.hangup_fails = True, True
        logs = []
        self.server.services.voice.log = logs.append
        call_id = self.ringing()
        status, body = self.request("POST", f"/v1/calls/{call_id}/accept", {})
        self.assertEqual((status, body["error"]), (503, "brain_voice_unavailable"))
        self.assertEqual(len(self.hangups()), 1)
        self.assertIn("收 brain 那頭也失敗", " ".join(logs))

    def test_definite_busy_releases_only_this_call_id(self):
        """brain 明確回忙（她在別通裡）⇒ 收尾的 hangup 帶的是這一通的 id，brain 那邊不會誤掛別通。"""
        self.brain.refuse = "busy_elsewhere"
        call_id = self.ringing()
        self.request("POST", f"/v1/calls/{call_id}/accept", {})
        self.assertTrue(all(payload["call_id"] == call_id for payload in self.hangups()))

    def test_answer_and_hangup_use_the_short_timeout(self):
        """審核 OC-2：`answer` 不再吃 180 秒——brain 卡住時最多等 RING_TIMEOUT_S 就轉 failed。"""
        import urllib.request
        from unittest import mock
        from app.server import voice as voice_module
        bridge = voice_module.BrainVoiceBridge("http://brain.invalid", "secret", timeout=180.0)
        seen = {}

        def fake_open(request, timeout=0):
            seen[__import__("json").loads(request.data)["action"]] = timeout
            raise OSError("timed out")

        with mock.patch.object(urllib.request, "urlopen", fake_open):
            for action in ("answer", "hangup", "utterance"):
                with self.assertRaises(BoardError):
                    bridge.call(action, {"call_id": "c"})
        self.assertEqual(seen["answer"], voice_module.RING_TIMEOUT_S)
        self.assertEqual(seen["hangup"], voice_module.RING_TIMEOUT_S)
        self.assertEqual(seen["utterance"], 180.0)                 # 一般回合照舊

    def test_old_brain_without_answer_falls_back_to_start(self):
        self.brain.old = True
        call_id = self.ringing()
        status, body = self.request("POST", f"/v1/calls/{call_id}/accept", {})
        self.assertEqual((status, body["state"]), (200, "live"))
        self.assertIn("start", [action for action, _ in self.brain.calls])


if __name__ == "__main__":
    unittest.main()
