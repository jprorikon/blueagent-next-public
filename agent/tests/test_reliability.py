#!/usr/bin/env python3
"""一、1+2+3：SSE 重播（Last-Event-ID）、主人指令佇列、告警。

判據：
  ① 匯流排每條訊息有單調 id；`replay(principal, last_id)` 只回「漏掉的那些」；
     http 層帶 `Last-Event-ID` / `?since=` 都會補；
  ② 指令佇列：同一個角色嚴格 FIFO、一次只跑一條（不同角色可並行）、有狀態可查、
     滿了拒收且**不靜默丢**；
  ③ 告警：樣本不足不報、超閾值才報、同種告警有冷卻、冷卻狀態落盤（重啟不重複報）。
"""

from __future__ import annotations

import pathlib
import tempfile
import threading
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.ops.alerts import AlertMonitor
from agent.brain.runtime.delivery.bus import EventBus
from agent.brain.runtime.owner_queue import DONE, FAILED, OwnerCommandQueue


class TestBusReplay(unittest.TestCase):
    def test_ids_are_monotonic_and_replay_returns_only_missed(self):
        bus = EventBus()
        bus.publish("owner", {"kind": "delivery", "text": "一"})
        bus.publish("owner", {"kind": "delivery", "text": "二"})
        first_id = bus.last_id("owner")
        bus.publish("owner", {"kind": "delivery", "text": "三"})
        missed = bus.replay("owner", first_id)
        self.assertEqual([item["text"] for item in missed], ["三"], "只補斷線之後的")
        self.assertEqual(bus.replay("owner", 0), [], "第一次連線不需要補")
        self.assertEqual(bus.replay("someone-else", first_id), [], "別人的訊息不補給他")

    def test_history_is_bounded(self):
        bus = EventBus(history_max=3)
        for index in range(6):
            bus.publish("owner", {"kind": "delivery", "text": str(index)})
        missed = bus.replay("owner", 0 + 1)
        self.assertLessEqual(len(missed), 3, "只留最近幾條")
        self.assertEqual(missed[-1]["text"], "5")

    def test_live_subscriber_gets_envelope_with_id(self):
        bus = EventBus()
        sub_id, channel = bus.subscribe("owner")
        bus.publish("owner", {"kind": "delivery", "text": "やあ"})
        envelope = channel.get(timeout=1.0)
        self.assertGreater(int(envelope["id"]), 0)
        bus.unsubscribe("owner", sub_id)


class TestOwnerCommandQueue(unittest.TestCase):
    def setUp(self):
        self.done: list[tuple] = []
        self.gate = threading.Event()
        self.order: list[str] = []
        self.lock = threading.Lock()

    def runner(self, text, *, entity="", principal="owner", meta=None):
        # 第一條會卡住，用來驗「同角色不並行」
        if str(text).startswith("慢"):
            self.gate.wait(2.0)
        with self.lock:
            self.order.append(text)
            self.done.append((text, entity))
        return {"text": f"做完：{text}"}

    def test_same_entity_runs_one_at_a_time(self):
        queue = OwnerCommandQueue(self.runner)
        queue.start()
        queue.submit("慢 1", entity="mira")
        queue.submit("快 2", entity="mira")
        time.sleep(0.2)
        self.assertEqual(self.order, [], "第一條還沒做完，第二條不該跑")
        self.gate.set()
        deadline = time.time() + 3.0
        while len(self.order) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.order, ["慢 1", "快 2"], "同一個角色嚴格 FIFO")
        queue.stop()

    def test_different_entities_can_run_in_parallel(self):
        queue = OwnerCommandQueue(self.runner)
        queue.start()
        queue.submit("慢 A", entity="mira")
        queue.submit("快 B", entity="aster")
        # 5 s：整套測試跑滿載時 2 s 會不夠（實測偶發紅），這裡測的是「有沒有被擋」不是速度
        deadline = time.time() + 5.0
        while "快 B" not in self.order and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn("快 B", self.order, "別的角色不該被擋住")
        self.gate.set()
        queue.stop()

    def test_snapshot_reports_states_and_stats(self):
        queue = OwnerCommandQueue(self.runner)
        queue.start()
        queue.submit("做事", entity="mira")
        deadline = time.time() + 2.0
        while queue.snapshot()["stats"]["finished"] < 1 and time.time() < deadline:
            time.sleep(0.02)
        snap = queue.snapshot()
        self.assertEqual(snap["stats"]["finished"], 1)
        self.assertEqual(snap["recent"][-1]["state"], DONE)
        self.assertGreater(snap["recent"][-1]["result_chars"], 0)
        queue.stop()

    def test_failure_is_recorded_not_swallowed(self):
        def boom(text, **_kwargs):
            raise RuntimeError("壞了")

        queue = OwnerCommandQueue(boom, log=lambda _m: None)
        queue.start()
        queue.submit("會炸", entity="mira")
        deadline = time.time() + 2.0
        while queue.snapshot()["stats"]["failed"] < 1 and time.time() < deadline:
            time.sleep(0.02)
        snap = queue.snapshot()
        self.assertEqual(snap["stats"]["failed"], 1)
        self.assertEqual(snap["recent"][-1]["state"], FAILED)
        self.assertIn("RuntimeError", snap["recent"][-1]["error"])
        queue.stop()

    def test_full_queue_rejects_instead_of_dropping(self):
        queue = OwnerCommandQueue(self.runner, max_pending=1)
        # 不 start ⇒ 兩條都留在 pending（第一條卡在 pending，第二條就該被拒）
        self.assertTrue(queue.submit("一", entity="mira")["ok"])
        second = queue.submit("二", entity="mira")
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "queue_full")

    def test_empty_text_is_rejected(self):
        queue = OwnerCommandQueue(self.runner)
        self.assertFalse(queue.submit("   ")["ok"])


class FakeRecorder:
    def __init__(self, metrics):
        self._metrics = metrics

    def metrics(self):
        return dict(self._metrics)


class TestAlerts(unittest.TestCase):
    def monitor(self, metrics, sent=None, **kwargs) -> AlertMonitor:
        sent = sent if sent is not None else []
        settings = dict(interval_s=5, cooldown_s=60, latency_p95_ms=20_000,
                        speech_error_rate=0.3, failure_rate=0.3, min_turns=20,
                        log=lambda _m: None)
        settings.update(kwargs)
        return AlertMonitor(FakeRecorder(metrics),
                            lambda text: sent.append(text) or True, **settings)

    def test_small_sample_is_ignored(self):
        monitor = self.monitor({"turns": 3, "failures": 3, "speech_errors": 3,
                                "latency_ms": {"p95": 90_000}})
        self.assertEqual(monitor.evaluate(), [], "樣本不足不該報")

    def test_slow_and_broken_are_reported(self):
        monitor = self.monitor({"turns": 40, "failures": 0, "speech_errors": 20,
                                "latency_ms": {"p95": 35_000}})
        kinds = {item["kind"] for item in monitor.evaluate()}
        self.assertEqual(kinds, {"latency_p95", "speech_error_rate"})

    def test_healthy_system_is_silent(self):
        monitor = self.monitor({"turns": 40, "failures": 1, "speech_errors": 1,
                                "latency_ms": {"p95": 3_000}})
        self.assertEqual(monitor.evaluate(), [])

    def test_cooldown_and_persistence(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        metrics = {"turns": 40, "failures": 0, "speech_errors": 30,
                   "latency_ms": {"p95": 1_000}}
        sent: list[str] = []
        monitor = self.monitor(metrics, sent, state_path=tmp / "alerts.json",
                               clock=lambda: 10_000.0)
        self.assertEqual(len(monitor.check()), 1)
        self.assertEqual(len(monitor.check()), 0, "同一種告警冷卻中")
        again = self.monitor(metrics, sent, state_path=tmp / "alerts.json",
                             clock=lambda: 10_030.0)
        self.assertEqual(again.check(), [], "冷卻狀態要落盤：重啟不重複報")
        later = self.monitor(metrics, sent, state_path=tmp / "alerts.json",
                             clock=lambda: 12_000.0)
        self.assertEqual(len(later.check()), 1, "冷卻過了要能再報")

    def test_send_failure_rolls_back_cooldown(self):
        monitor = self.monitor({"turns": 40, "failures": 0, "speech_errors": 30,
                                "latency_ms": {"p95": 1_000}},
                               sent=None)
        monitor.send = lambda _text: False
        self.assertEqual(monitor.check(), [])
        self.assertEqual(monitor._last, {}, "送不出去不該記成已報")


class TestHttpRoutesExist(unittest.TestCase):
    def test_commands_endpoint_is_registered(self):
        from agent.server import api
        self.assertTrue(callable(getattr(api, "get_commands", None)))
        self.assertTrue(callable(getattr(api, "post_command", None)))

    def test_command_post_requires_owner(self):
        from agent.server import api
        status, body = api.post_command(object(), {"text": "做事"}, auth=None)
        self.assertEqual(status, 401)
        self.assertIn("error", body)


class TestAlertCalibrationDefaults(unittest.TestCase):
    """門檻現在是「用真數據標定」的（2026-10-22，473 輪）：不能又滑回 20 s / 30%。"""

    def test_defaults_match_calibration(self):
        from agent.brain.runtime.ops.alerts import AlertMonitor

        class Recorder:
            def windows(self, *_args, **_kwargs):
                return {}

        monitor = AlertMonitor(Recorder(), lambda _text: True)
        self.assertEqual(monitor.latency_p95_ms, 16_000.0)
        self.assertAlmostEqual(monitor.speech_error_rate, 0.15)
        self.assertAlmostEqual(monitor.failure_rate, 0.15)

    def test_calibration_tool_exists(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        self.assertTrue((root / "agent" / "tools" / "alert_calibration.py").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
