#!/usr/bin/env python3
"""主人指令佇列：落盤、取消、上限、種類骨架（owner 2026-11-16 定案，PENDING B.Agent.0）。

判據：
  ① 同一個角色未完成最多 10 條，第 11 條拒收；別的角色不受影響；
  ② `kind`：只有 work 會跑；trpg／game／read 回 kind_not_implemented，不認得的回 kind_unknown；
  ③ 取消在步邊界停：排隊中立刻 cancelled 且不跑；執行中的那一步跑完才停、結果不交付；
     已結束的回 already_finished，找不到回 not_found；
  ④ 排隊超過 24 小時作廢（expired），不會被拿去跑；
  ⑤ 重啟：排隊的照跑、編號接著往下；交付回呼存不了 ⇒ 改走建構時給的 deliver；
     回呼之類存不了的 meta 被丟掉；跑到一半被打斷的標 failed（interrupted_by_restart），不重跑；
  ⑥ API：kind 未實作 ⇒ 501；取消要 owner；狀態碼對得上；
  ⑦ 「步」細到工具呼叫（2026-11-16）：執行中取消會呼叫執行本體登記的停止函式（登記晚於取消就當場呼叫，
     登記函式不落盤）；真跑一輪 work：兩次工具呼叫之間取消 ⇒ 第二支工具不跑、結果不交付。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.owner_queue import (CANCEL_HOOK, CANCELLED, DONE, EXPIRED, FAILED,
                                             QUEUED, OwnerCommandQueue)


def wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "owner_commands.json"
        self.ran: list[str] = []
        self.gate = threading.Event()
        self.now = [1_000_000.0]

    def runner(self, text, *, entity="", principal="owner", meta=None):
        if text.startswith("慢"):
            self.gate.wait(5.0)
        self.ran.append(text)
        return {"text": f"做完：{text}"}

    def queue(self, **kwargs) -> OwnerCommandQueue:
        kwargs.setdefault("store_path", self.path)
        kwargs.setdefault("clock", lambda: self.now[0])
        queue = OwnerCommandQueue(self.runner, **kwargs)
        self.addCleanup(queue.stop)
        return queue

    @staticmethod
    def state_of(queue, command_id) -> str:
        snap = queue.snapshot(limit=50)
        for row in snap["queued"] + snap["running"] + snap["recent"]:
            if row["id"] == command_id:
                return row["state"]
        return ""


class TestInboxPreview(Base):
    """Unity 收件箱（2026-11-17）要看得出是哪一件：指令開頭、結果開頭；結果開頭重啟後還在。"""

    def test_preview_and_result_preview(self):
        queue = self.queue()
        queue.start()
        long_text = "観測ログを整理して" + "あ" * 200
        sent = queue.submit(long_text, entity="aster")
        self.assertTrue(wait_for(lambda: self.state_of(queue, sent["id"]) == "done"))
        row = queue.snapshot()["recent"][-1]
        self.assertEqual(row["preview"], long_text[:80])
        self.assertEqual(row["chars"], len(long_text))
        self.assertTrue(row["result_preview"].startswith("做完：観測ログを整理して"))
        self.assertLessEqual(len(row["result_preview"]), 160)
        queue.stop()
        again = self.queue()
        self.assertEqual(again.snapshot()["recent"][-1]["result_preview"], row["result_preview"])


class TestLimitsAndKinds(Base):
    def test_ten_open_per_entity(self):
        queue = self.queue()
        for index in range(10):
            self.assertTrue(queue.submit(f"仕事{index}", entity="mira")["ok"])
        eleventh = queue.submit("仕事10", entity="mira")
        self.assertEqual((eleventh["ok"], eleventh["reason"]), (False, "queue_full"))
        self.assertEqual(eleventh["open_for_entity"], 10)
        self.assertTrue(queue.submit("別の子", entity="aster")["ok"], "別的角色不受影響")

    def test_cancelled_items_free_a_slot(self):
        queue = self.queue(max_per_entity=1)
        first = queue.submit("一", entity="mira")
        self.assertFalse(queue.submit("二", entity="mira")["ok"])
        queue.cancel(first["id"])
        self.assertTrue(queue.submit("二", entity="mira")["ok"])

    def test_kinds(self):
        queue = self.queue()
        self.assertTrue(queue.submit("仕事", kind="work")["ok"])
        for kind in ("trpg", "game", "read"):
            self.assertEqual(queue.submit("x", kind=kind)["reason"], "kind_not_implemented", kind)
        self.assertEqual(queue.submit("x", kind="dance")["reason"], "kind_unknown")


class TestCancel(Base):
    def test_cancel_queued_never_runs(self):
        queue = self.queue()
        first = queue.submit("消す", entity="mira")
        outcome = queue.cancel(first["id"])
        self.assertEqual(outcome["state"], CANCELLED)
        queue.submit("残す", entity="mira")
        queue.start()
        self.assertTrue(wait_for(lambda: self.ran == ["残す"]))
        self.assertEqual(self.state_of(queue, first["id"]), CANCELLED)

    def test_cancel_running_stops_at_the_step_boundary(self):
        delivered = []
        queue = self.queue()
        queue.start()
        item = queue.submit("慢い仕事", entity="mira", on_done=delivered.append)
        self.assertTrue(wait_for(lambda: self.state_of(queue, item["id"]) == "running"))
        outcome = queue.cancel(item["id"])
        self.assertEqual((outcome["state"], outcome["cancel_requested"]), ("running", True))
        self.assertEqual(self.ran, [], "不殺正在跑的那一步")
        self.gate.set()
        self.assertTrue(wait_for(lambda: self.state_of(queue, item["id"]) == CANCELLED))
        self.assertEqual(self.ran, ["慢い仕事"], "那一步跑完了")
        self.assertEqual(delivered, [], "取消了 ⇒ 結果不交付")
        self.assertEqual(queue.snapshot()["stats"]["cancelled"], 1)

    def test_cancel_finished_or_unknown(self):
        queue = self.queue()
        queue.start()
        item = queue.submit("すぐ", entity="mira")
        self.assertTrue(wait_for(lambda: self.state_of(queue, item["id"]) == DONE))
        self.assertEqual(queue.cancel(item["id"])["reason"], "already_finished")
        self.assertEqual(queue.cancel(999)["reason"], "not_found")
        self.assertEqual(queue.cancel("abc")["reason"], "not_found")


class TestExpiry(Base):
    def test_older_than_a_day_is_expired_not_run(self):
        queue = self.queue()
        old = queue.submit("古い", entity="mira")
        self.now[0] += 24 * 3600
        fresh = queue.submit("新しい", entity="mira")
        self.assertEqual(self.state_of(queue, old["id"]), EXPIRED)
        queue.start()
        self.assertTrue(wait_for(lambda: self.ran == ["新しい"]))
        self.assertEqual(self.state_of(queue, fresh["id"]), DONE)


class TestRestart(Base):
    def test_queued_items_survive_and_ids_continue(self):
        first = self.queue()
        a = first.submit("一", entity="mira", source="api:command",
                         meta={"note": "メモ", "on_speech_frame": print})
        b = first.submit("二", entity="aster", on_done=lambda _r: None)
        first.stop()
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([row["state"] for row in stored["items"]], [QUEUED, QUEUED])
        self.assertEqual(stored["items"][0]["meta"], {"note": "メモ"}, "回呼存不了 ⇒ 丟掉")

        delivered = []
        second = self.queue(deliver=lambda item, result: delivered.append((item["id"], result["text"])))
        self.assertEqual([row["id"] for row in second.snapshot()["queued"]], [a["id"], b["id"]])
        c = second.submit("三", entity="nori")
        self.assertEqual(c["id"], b["id"] + 1, "編號接著往下，不跟重啟前撞號")
        second.start()
        self.assertTrue(wait_for(lambda: len(delivered) == 3))
        self.assertEqual(sorted(delivered), [(a["id"], "做完：一"), (b["id"], "做完：二"),
                                             (c["id"], "做完：三")],
                         "重啟前的交付回呼沒了 ⇒ 走 deliver；重啟後新排的沒有 on_done 也走 deliver")

    def test_running_at_crash_is_failed_not_rerun(self):
        self.path.write_text(json.dumps({"next_id": 8, "items": [
            {"id": 7, "text": "半分やった", "entity": "mira", "principal": "owner",
             "source": "api:command", "kind": "work", "meta": {}, "state": "running",
             "queued_at": self.now[0] - 60, "started_at": self.now[0] - 30, "finished_at": None,
             "error": "", "result_chars": 0, "cancel_requested": False},
            {"id": 6, "text": "済み", "entity": "mira", "principal": "owner", "source": "",
             "kind": "work", "meta": {}, "state": "done", "queued_at": 1.0, "started_at": 2.0,
             "finished_at": 3.0, "error": "", "result_chars": 4, "cancel_requested": False},
        ]}), encoding="utf-8")
        queue = self.queue()
        queue.start()
        time.sleep(0.3)
        self.assertEqual(self.ran, [], "可能已經做了一半 ⇒ 不重跑")
        recent = {row["id"]: row for row in queue.snapshot(limit=10)["recent"]}
        self.assertEqual((recent[7]["state"], recent[7]["error"]), (FAILED, "interrupted_by_restart"))
        self.assertEqual(recent[6]["state"], DONE, "已結束的照留")
        self.assertEqual(queue.submit("次", entity="mira")["id"], 8)

    def test_unreadable_store_starts_empty(self):
        self.path.write_text("{壊れた", encoding="utf-8")
        queue = self.queue()
        self.assertEqual(queue.snapshot()["pending"], 0)
        self.assertTrue(queue.submit("動く", entity="mira")["ok"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["items"][0]["text"], "動く")


class TestApi(Base):
    def owner(self):
        from agent.brain.domain.auth import AuthContext, Role
        return AuthContext(principal="owner", role=Role.OWNER)

    def state(self):
        class State:
            pass
        state = State()
        state.commands = self.queue()
        return state

    def test_post_command_status_codes(self):
        from agent.server import api
        state = self.state()
        status, body = api.post_command(state, {"text": "卓で遊ぶ", "kind": "trpg"}, auth=self.owner())
        self.assertEqual((status, body["error"]), (501, "kind_not_implemented"))
        status, body = api.post_command(state, {"text": "x", "kind": "dance"}, auth=self.owner())
        self.assertEqual(status, 400)
        status, body = api.post_command(state, {"text": "仕事"}, auth=self.owner())
        self.assertEqual(status, 202)

    def test_cancel_route(self):
        from agent.server import api
        state = self.state()
        status, body = api.post_command_cancel(state, {"id": 1}, auth=None)
        self.assertEqual(status, 401)
        _status, sent = api.post_command(state, {"text": "仕事"}, auth=self.owner())
        status, body = api.post_command_cancel(state, {"id": sent["command_id"]}, auth=self.owner())
        self.assertEqual((status, body["state"]), (200, CANCELLED))
        status, _body = api.post_command_cancel(state, {"id": sent["command_id"]}, auth=self.owner())
        self.assertEqual(status, 409)
        status, _body = api.post_command_cancel(state, {"id": 404}, auth=self.owner())
        self.assertEqual(status, 404)
        status, _body = api.dispatch_post(state, "/api/command/cancel", {"id": 1}, auth=self.owner())
        self.assertEqual(status, 409, "dispatch 認得這條路由")


class TestCancelHook(Base):
    def test_running_cancel_calls_the_registered_stop(self):
        stopped = threading.Event()
        started = threading.Event()

        def runner(text, *, entity="", principal="owner", meta=None):
            meta[CANCEL_HOOK](stopped.set)
            started.set()
            stopped.wait(5.0)                  # 「工具迴圈」等令牌翻起來就停
            return {"text": "途中で止めた"}

        queue = OwnerCommandQueue(runner, store_path=self.path, clock=lambda: self.now[0])
        self.addCleanup(queue.stop)
        queue.start()
        item = queue.submit("長い仕事", entity="mira")
        self.assertTrue(started.wait(5.0))
        stored = json.loads(self.path.read_text(encoding="utf-8"))["items"][0]
        self.assertNotIn(CANCEL_HOOK, stored["meta"], "登記函式不落盤")
        self.assertNotIn("cancel_hooks", stored)
        queue.cancel(item["id"])
        self.assertTrue(stopped.is_set(), "取消當下就翻令牌，不等那一步自己跑完")
        self.assertTrue(wait_for(lambda: self.state_of(queue, item["id"]) == CANCELLED))

    def test_registering_after_cancel_stops_at_once(self):
        calls = []
        go = threading.Event()

        def runner(text, *, entity="", principal="owner", meta=None):
            go.wait(5.0)
            meta[CANCEL_HOOK](lambda: calls.append("stop"))
            return {"text": "x"}

        queue = OwnerCommandQueue(runner, store_path=self.path, clock=lambda: self.now[0])
        self.addCleanup(queue.stop)
        queue.start()
        item = queue.submit("仕事", entity="mira")
        self.assertTrue(wait_for(lambda: self.state_of(queue, item["id"]) == "running"))
        queue.cancel(item["id"])
        go.set()
        self.assertTrue(wait_for(lambda: self.state_of(queue, item["id"]) == CANCELLED))
        self.assertEqual(calls, ["stop"], "登記晚於取消 ⇒ 當場呼叫")


class TestCancelBetweenToolCalls(unittest.TestCase):
    """真跑一輪 work：第一支工具跑完、第二支還沒開始時取消 ⇒ 第二支不跑。"""

    def test_second_tool_never_runs(self):
        import os
        import demo_common
        from agent.brain.compose.stages import brain as brain_stage
        from agent.brain.compose.state import AppState

        os.environ.setdefault("BLUEAGENT_BRAIN", "mock")
        data_dir = tempfile.mkdtemp(prefix="ba-cmd-cancel-")
        app = demo_common.build(brain_delay=0.0, data_dir=data_dir, step_duration=0.02,
                                life_pause=0.005, seed=False)
        state = AppState(app)
        self.addCleanup(state.commands.stop)
        ran: list[str] = []
        between = threading.Event()
        resume = threading.Event()
        original = brain_stage.BrainStage._run_tool

        def recording(stage, env, turn, ctx, payload, call_id):
            ran.append(str(payload.get("arguments", {}).get("query")))
            return original(stage, env, turn, ctx, payload, call_id)

        brain_stage.BrainStage._run_tool = recording
        self.addCleanup(setattr, brain_stage.BrainStage, "_run_tool", original)

        def run_turn(_request):
            yield "tool_call", {"id": "c1", "name": "memory_search", "arguments": {"query": "一つ目"}}
            between.set()
            resume.wait(5.0)
            yield "tool_call", {"id": "c2", "name": "memory_search", "arguments": {"query": "二つ目"}}
            yield "done", {"text": "終わった"}

        app.brain.run_turn = run_turn
        delivered = []
        sent = state.commands.submit("調べて", entity="aster", on_done=delivered.append)
        self.assertTrue(sent["ok"], sent)
        self.assertTrue(between.wait(10.0), "第一支工具之後")
        self.assertEqual(state.commands.cancel(sent["id"])["state"], "running")
        resume.set()
        self.assertTrue(wait_for(lambda: Base.state_of(state.commands, sent["id"]) == CANCELLED, 10.0))
        self.assertEqual(ran, ["一つ目"], "第二支工具沒有跑")
        self.assertEqual(delivered, [], "取消了 ⇒ 不交付")


if __name__ == "__main__":
    unittest.main(verbosity=2)
