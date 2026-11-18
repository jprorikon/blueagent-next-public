#!/usr/bin/env python3
"""MR 頭顯自己一條管道（owner 2026-11-01「MR 端走的也是 app，會和現在的 app 管道混在一起」）。

判據：
  ① `X-Source: mr` 的通話一句標成 `mr:call`（手機仍是 `phone:call`）；算通話、算實時源、工具面是 call；
  ② 通話：MR 撥＝`end=mr` 的一通；**後撥的蓋掉先撥的**（手機 ⇄ MR 兩個方向都是），被蓋掉的那一通心跳 410；
     Discord 語音房照舊 `busy_elsewhere`；MR 的通話不在手機紀錄留標記；
     被蓋掉的那一端心跳／送一句拿 `410 taken_over`，其他原因沒了的照舊 `call_ended`（WO §7 W3）；
  ③ 推播照來源送回那一端：`mr:` 只給 MR；其他（含主動訊息）只給手機；重播也一樣；
  ④ 手機查紀錄（`source=phone,api`）看不到 MR 的那幾輪；
  ⑤ MR 通話中也有舞台（`motion`），手機通話沒有。
"""

from __future__ import annotations

import json
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.motion import MotionPlanner
from agent.brain.compose.turns import source_class_for
from agent.brain.domain.auth import AuthContext, Role
from agent.brain.domain.client_end import MR, PHONE, end_of, end_of_source, is_call
from agent.brain.domain.event import SourceClass
from agent.brain.runtime.delivery.bus import EventBus
from agent.brain.runtime.tools.channels import CALL, channel_of
from agent.brain.runtime.voice import ENDED_TTL_S
from agent.server import api as api_module
from test_audio_input import FakeSTT, wav_bytes

OWNER = AuthContext(principal="owner", role=Role.OWNER, capabilities=frozenset({"chat"}))


class TestClientEnd(unittest.TestCase):
    def test_header_to_end(self):
        self.assertEqual(end_of("mr"), MR)
        self.assertEqual(end_of("MR"), MR)
        self.assertEqual(end_of("phone"), PHONE)
        self.assertEqual(end_of(""), PHONE)                  # 舊包不送 X-Source
        self.assertEqual(end_of("mri"), PHONE)               # 只認整個前綴

    def test_source_to_end_and_call(self):
        self.assertEqual(end_of_source("mr:call"), MR)
        self.assertEqual(end_of_source("phone:call"), PHONE)
        self.assertEqual(end_of_source(""), PHONE)           # 主動訊息
        self.assertTrue(is_call("mr:call"))
        self.assertTrue(is_call("phone:call"))
        self.assertFalse(is_call("mr:audio"))
        self.assertEqual(channel_of("mr:call"), CALL)
        self.assertIs(source_class_for("mr:call"), SourceClass.REALTIME)

    def test_mr_call_has_a_stage_phone_call_does_not(self):
        planner = MotionPlanner(library=object())
        self.assertTrue(planner.on_stage("mr:call"))
        self.assertFalse(planner.on_stage("phone:call"))
        self.assertTrue(planner.on_stage("phone:owner"))


class CallCase(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.voice = self.state.app.voice

    def post(self, action, body=None, source=""):
        return api_module.dispatch_post(self.state, f"/api/voice/{action}",
                                        dict(body or {"entity": "mira"}), auth=OWNER, source=source)


class TestCallTakeover(CallCase):
    def test_mr_call_is_its_own_end(self):
        status, data = self.post("call", source="mr")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["session"]["end"], "mr")

    def test_later_end_takes_over_both_ways(self):
        _, phone = self.post("call")
        _, mr = self.post("call", source="mr")
        self.assertEqual(mr["session"]["end"], "mr")
        self.assertNotEqual(mr["session"]["id"], phone["session"]["id"])
        # 手機那一通沒了：它下一次心跳拿 410、自己收線
        status, _ = self.post("ping", {"entity": "mira", "call_id": phone["session"]["id"]})
        self.assertEqual(status, 410)
        self.assertEqual(self.voice.floor.holder("mira"), mr["session"]["id"])

        _, again = self.post("call")
        self.assertEqual(again["session"]["end"], "phone")
        status, _ = self.post("ping", {"entity": "mira", "call_id": mr["session"]["id"]}, source="mr")
        self.assertEqual(status, 410)

    def test_same_end_is_still_idempotent(self):
        _, first = self.post("call", source="mr")
        _, second = self.post("call", source="mr")
        self.assertTrue(second["existing"])
        self.assertEqual(second["session"]["id"], first["session"]["id"])

    def test_discord_room_is_not_taken_over(self):
        self.voice.call("mira", end="discord", principal="owner")
        status, data = self.post("call", source="mr")
        self.assertEqual(status, 409)
        self.assertEqual(data["error"], "busy_elsewhere")

    def test_mr_call_audio_is_tagged_mr(self):
        self.state.app.stt = FakeSTT()
        _, data = self.post("call", source="mr")
        call_id = data["session"]["id"]
        status, data = api_module.prepare_audio(
            self.state, wav_bytes(), headers={"X-Call": call_id, "X-Entity": "mira"},
            auth=OWNER, source="mr")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["source_override"], "mr:call")

    def test_mr_call_leaves_no_marker_in_phone_history(self):
        _, data = self.post("call", source="mr")
        self.voice._sessions["mira"].created_at -= 60      # 夠長，手機的通話會留標記
        self.post("hangup", {"entity": "mira", "call_id": data["session"]["id"]}, source="mr")
        rows = self.state.app.outbox.since("", principal="owner", limit=50)
        self.assertFalse([row for row in rows if row.get("kind") == "call-log"], rows)


class TestTakenOverReason(CallCase):
    """WO-mr-call-and-switch §7 W3：被接管的那一端要分得出「被接管」與「自己斷了」。"""

    def ping(self, call_id, source=""):
        return self.post("ping", {"entity": "mira", "call_id": call_id}, source=source)

    def test_both_directions_say_taken_over(self):
        _, phone = self.post("call")
        _, mr = self.post("call", source="mr")
        status, data = self.ping(phone["session"]["id"])
        self.assertEqual((status, data["error"]), (410, "taken_over"))
        self.post("call")
        status, data = self.ping(mr["session"]["id"], source="mr")
        self.assertEqual((status, data["error"]), (410, "taken_over"))

    def test_sentence_after_takeover_says_taken_over(self):
        self.state.app.stt = FakeSTT()
        _, mr = self.post("call", source="mr")
        self.post("call")
        status, data = api_module.prepare_audio(
            self.state, wav_bytes(), headers={"X-Call": mr["session"]["id"], "X-Entity": "mira"},
            auth=OWNER, source="mr")
        self.assertEqual((status, data["error"]), (410, "taken_over"))

    def test_idle_timeout_is_not_taken_over(self):
        _, phone = self.post("call")
        self.voice.active("mira").last_seen = time.time() - self.voice.call_idle_s - 1
        status, data = self.ping(phone["session"]["id"])
        self.assertEqual((status, data["error"]), (410, "call_ended"))

    def test_plain_hangup_is_not_taken_over(self):
        _, phone = self.post("call")
        self.post("hangup", {"entity": "mira", "call_id": phone["session"]["id"]})
        status, data = self.ping(phone["session"]["id"])
        self.assertEqual((status, data["error"]), (410, "call_ended"))

    def test_reason_expires(self):
        _, phone = self.post("call")
        self.post("call", source="mr")
        reason, at = self.voice._ended[phone["session"]["id"]]
        self.voice._ended[phone["session"]["id"]] = (reason, at - ENDED_TTL_S - 1)
        status, data = self.ping(phone["session"]["id"])
        self.assertEqual((status, data["error"]), (410, "call_ended"))


class TestHangupStaysLight(CallCase):
    """掛斷回應要輕（2026-11-01 實機：附整份狀態＋音檔兩份 ⇒ 200 要 11–31 秒才到）。"""

    def test_hangup_has_no_full_state_and_no_audio(self):
        self.voice._last_speech = {"text": "またね", "audio": "A" * 400_000, "duration_ms": 900}
        _, data = self.post("call")
        status, out = self.post("hangup", {"entity": "mira", "call_id": data["session"]["id"]})
        self.assertEqual(status, 200)
        self.assertNotIn("state", out)
        self.assertEqual(out["voice"]["last_speech"]["text"], "またね")
        self.assertNotIn("audio", out["voice"]["last_speech"])
        self.assertLess(len(json.dumps(out, ensure_ascii=False)), 20_000)
        self.assertNotIn("audio", self.state.snapshot(entity="mira")["voice"]["last_speech"])


class TestBusRouting(unittest.TestCase):
    def test_deliveries_go_back_to_their_end(self):
        bus = EventBus()
        _, phone = bus.subscribe("owner")
        _, mr = bus.subscribe("owner", MR)
        bus.publish("owner", {"kind": "delivery", "text": "MR の続き", "source": "mr:call"})
        bus.publish("owner", {"kind": "delivery", "text": "スマホの続き", "source": "api:audio"})
        bus.publish("owner", {"kind": "delivery", "text": "ねえオーナー"})              # 主動訊息：沒有來源
        self.assertEqual([phone.get_nowait()["text"] for _ in range(phone.qsize())],
                         ["スマホの続き", "ねえオーナー"])
        self.assertEqual([mr.get_nowait()["text"] for _ in range(mr.qsize())], ["MR の続き"])

    def test_replay_is_per_end(self):
        bus = EventBus()
        bus.publish("owner", {"kind": "delivery", "text": "a", "source": "mr:call"})
        bus.publish("owner", {"kind": "delivery", "text": "b", "source": "phone:owner"})
        self.assertEqual([item["text"] for item in bus.replay("owner", 1, MR)], ["a"])
        self.assertEqual([item["text"] for item in bus.replay("owner", 1)], ["b"])
        self.assertNotIn("_end", bus.replay("owner", 1)[0])

    def test_counts_per_end(self):
        bus = EventBus()
        bus.subscribe("owner", MR)
        self.assertEqual(bus.counts()["subscribers"], 1)
        self.assertEqual(bus.counts(PHONE)["subscribers"], 0)


class TestHistorySeparation(unittest.TestCase):
    def test_phone_history_does_not_show_mr_turns(self):
        state = make_state()
        self.addCleanup(state.shutdown)
        outbox = state.app.outbox
        outbox.record(turn_id="t1", principal="owner", entity="mira", text="MR", source="mr:audio")
        outbox.record(turn_id="t2", principal="owner", entity="mira", text="phone", source="phone:owner")
        status, data = api_module.list_turns(
            state, {"entity": ["mira"], "source": ["phone,api"]}, auth=OWNER)
        self.assertEqual(status, 200, data)
        texts = [row.get("text") for row in data.get("turns", [])]
        self.assertIn("phone", texts)
        self.assertNotIn("MR", texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
