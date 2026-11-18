#!/usr/bin/env python3
"""群聊回合移出拉取线程（PENDING B.App.2 剩余 ③，2026-11-16）。

判据：
  ① 生产形状（`start()`）：一轮群聊回合卡住 ⇒ 拉取线程照样消费后续事件（私聊照常回、游标推进）；
  ② 回合跑的期间新入队的条目不会被 `tick` 结束时的写回吃掉（旧写法「读一次、跑完、写回 remaining」）；
  ③ 零延迟的群聊也入队（拉取线程不跑群聊模型回合）；
  ④ 侧档（unsent／dead-letter）两条线程同时写不打架；
  ⑤ `close()` 两条线程都收。
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest import mock

import test_app_chat_adapter as base
from test_app_chat_adapter import FakeAttention, FakeClient, event

from agent.adapter import app_group_delay
from agent.adapter.app_adapter_store import AdapterSidecar
from agent.adapter.app_server import AppChatAdapter, GroupLedger


def wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestTurnWorker(unittest.TestCase):
    # 借用既有 adapter 測試的夾具（不繼承：繼承會把那一整組測試再跑一遍）
    ALIASES = base.AppChatAdapterCase.ALIASES
    setUp = base.AppChatAdapterCase.setUp
    result = base.AppChatAdapterCase.result
    adapter = base.AppChatAdapterCase.adapter
    group_adapter = base.AppChatAdapterCase.group_adapter

    def live_adapter(self, client, send):
        with mock.patch.dict(os.environ, {"BLUEAGENT_APP_POLL_IDLE_S": "0.2"}):
            adapter = AppChatAdapter(
                "aster", client, send, self.cursor,
                lambda result: str(result["frames"][-1]["payload"]["text"]),
                lambda result: list(result["frames"][-1]["payload"]["bubbles"]),
                group_attention=FakeAttention(seen=True),
                group_ledger=GroupLedger(self.root / "worker-ledger.json"),
                aliases=self.ALIASES)
        self.addCleanup(adapter.close)
        return adapter

    def test_pull_thread_keeps_going_while_a_group_turn_runs(self):
        group = event(cursor="1", text="みんなどう思う？", event_id="evt_g", conversation_kind="group")
        direct = event(cursor="2", text="ちょっといい？", event_id="evt_d")
        client = FakeClient([group, direct])
        gate = threading.Event()
        seen: list[str] = []

        def send(text, **kwargs):
            if kwargs.get("group"):
                gate.wait(10.0)                   # 群聊这一轮的模型很慢
            seen.append("group" if kwargs.get("group") else "direct")
            return self.result("うん。", ["うん。"])

        adapter = self.live_adapter(client, send)
        adapter.start()
        self.assertTrue(wait_for(lambda: "direct" in seen), "群聊回合卡着，私聊照样回")
        self.assertNotIn("group", seen)
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "2", "游标照推进")
        self.assertEqual(app_group_delay.pending_rooms(adapter), {"cnv_1"}, "群聊那轮还在队列里")
        gate.set()
        self.assertTrue(wait_for(lambda: "group" in seen))
        self.assertTrue(wait_for(lambda: app_group_delay.pending_rooms(adapter) == set()))
        self.assertEqual([row["conversation_id"] for row in client.sent], ["cnv_1", "cnv_1"])
        adapter.close()
        self.assertIsNone(adapter._thread)
        self.assertIsNone(adapter._turn_thread)

    def test_items_enqueued_during_a_turn_are_not_lost(self):
        now = [1762387200.0]
        client = FakeClient([event(cursor="1", text="最初の話", conversation_kind="group",
                                   created_at=now[0])])
        adapter = self.adapter(client, clock=lambda: now[0],
                               attention=FakeAttention(seen=True, delay=30.0),
                               ledger=GroupLedger(self.root / "lost-ledger.json",
                                                  clock=lambda: now[0]))

        def send(text, **kwargs):
            # 这一轮（延迟到期、由 tick 跑）跑的时候拉取线程又入队了一条，还没到期
            app_group_delay.hold(adapter, {"event_id": "later"}, conversation_id="cnv_1",
                                 message_id="msg_later", at=now[0], delay=60.0,
                                 granted=False, reason="ok", from_ai=True)
            return self.result("うん。", ["うん。"])

        adapter.send = send
        adapter.poll_once()                        # 入队（30 秒后到期）
        self.assertEqual([row["key"] for row in app_group_delay._load(adapter)], ["msg_1"])
        now[0] += 31
        self.assertEqual(app_group_delay.tick(adapter, now[0]), 1)
        self.assertEqual([row["key"] for row in app_group_delay._load(adapter)], ["msg_later"],
                         "跑完那一轮只移掉它自己，不把整份写回")

    def test_zero_delay_group_turn_is_queued_not_run_in_the_pull_thread(self):
        client = FakeClient([event(cursor="1", text="おーい", conversation_kind="group")])
        adapter = self.group_adapter(client, "aster", GroupLedger(self.root / "zero.json"))
        adapter._turn_thread = object()            # 假装工作线程在：poll_once 不当场结算
        adapter.poll_once()
        self.assertEqual(self.calls, [], "拉取线程不跑群聊回合")
        self.assertEqual(app_group_delay.pending_rooms(adapter), {"cnv_1"})
        adapter._turn_thread = None
        self.assertEqual(app_group_delay.tick(adapter, time.time()), 1)
        self.assertEqual(len(self.calls), 1)


class TestSidecarFromTwoThreads(unittest.TestCase):
    def test_concurrent_writes_do_not_collide(self):
        import pathlib
        import tempfile
        folder = tempfile.TemporaryDirectory(prefix="ba-sidecar-")
        self.addCleanup(folder.cleanup)
        sidecar = AdapterSidecar(pathlib.Path(folder.name) / "aster-cursor.json")
        errors: list[BaseException] = []

        def hammer(prefix: str):
            try:
                for index in range(150):
                    key = f"{prefix}{index}"
                    sidecar.put_unsent(key, {"parts": ["x"]})
                    sidecar.note_failure(key)
                    sidecar.drop_unsent(key)
                    sidecar.clear_failure(key)
            except BaseException as exc:         # noqa: BLE001 - 收集起來給斷言
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(name,)) for name in ("a", "b", "c")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30.0)
        self.assertEqual(errors, [])
        self.assertEqual(sidecar.unsent, {})
        self.assertEqual(AdapterSidecar(sidecar.cursor_path).unsent, {}, "落盘的也一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)
