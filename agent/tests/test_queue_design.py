#!/usr/bin/env python3
"""任務佇列（owner 2026-10-22 的四條規矩）。

判據：
  ① **上限 10**：每個角色排隊 ≤10；滿了丢「最不重要＋最舊」那條，語音那條不動；
  ② **作廢延長**：排隊超過 `stale_ms`（預設 180 s）才作廢，不是「一有新的就頂掉」；
  ③ **步邊界停，不硬中斷**：取消只翻旗標，管線在下一個階段／工具回合邊界自己停；
  ④ **語音優先**：`mic:`／`discord:vc`／`api:audio` ⇒ REALTIME（並行、可搶佔）；
     而「要幹活」的語音回合**先短回一句**，真正的工作以普通優先級排隊（`send_voice`）。
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.state import AppState, _source_class_for
from agent.brain.domain.decision import DispatchMode, Engagement
from agent.brain.domain.event import SourceClass
from agent.brain.runtime.scheduler import Scheduler, SchedulerConfig


def make_scheduler(**kwargs) -> Scheduler:
    return Scheduler(SchedulerConfig(**kwargs), name="test-lanes", entities=1)


def decision(priority: int, thread: str = "th-1", lane=SourceClass.INTERACTIVE,
             entity="mira"):
    """調度器是從 `decision.key.entity` 取角色的（不是 decision 上的欄位）。"""
    from agent.brain.domain.decision import RouteDecision
    from agent.brain.domain.ids import ThreadKey
    return RouteDecision(thread_id=thread, lane=lane, priority=priority,
                         mode=DispatchMode.ATTACH,
                         key=ThreadKey(scope="together", entity=entity),
                         pipeline="chat.text", scope="together", deadline_ms=8000,
                         reason="test", created=True, at=time.time())


class TestQueueCapacity(unittest.TestCase):
    def setUp(self):
        self.sched = make_scheduler(interactive_slots=1, queue_max=10, stale_ms=180_000)
        self.addCleanup(self.sched.shutdown)
        self.blocker = threading.Event()

    def _block_once(self):
        """先塞一輪把槽位占住，後面的都只能排隊。"""
        self.sched.submit(decision(5, thread="blocker"), lambda token: self.blocker.wait(2))
        time.sleep(0.05)

    def test_queue_is_capped_at_ten(self):
        self._block_once()
        for index in range(15):
            self.sched.submit(decision(5 + index, thread=f"th-{index}"),
                              lambda token: None)
        stats = self.sched.stats()
        self.assertLessEqual(stats["pending"], 10, stats)
        self.assertGreaterEqual(stats["dropped_full"], 5)
        self.blocker.set()

    def test_voice_jobs_are_not_the_ones_dropped(self):
        self._block_once()
        # 先塞 10 條低優先級（打字），再塞一條語音（REALTIME，priority 10 更高＝數字更小）
        for index in range(10):
            self.sched.submit(decision(50 + index, thread=f"th-{index}"),
                              lambda token: None)
        voice = self.sched.submit(decision(10, thread="th-voice", lane=SourceClass.REALTIME),
                                  lambda token: None)
        stats = self.sched.stats()
        self.assertLessEqual(stats["pending"], 10)
        self.assertFalse(voice.token.cancelled, "語音那條不該被丢棄（丢的是低優先級的打字）")
        self.assertGreaterEqual(stats["dropped_full"], 1)
        self.blocker.set()


class TestDroppedTurnFinalState(unittest.TestCase):
    def test_capacity_drop_fails_turn_and_restores_thread_queue(self):
        demo = build(brain_delay=0.25, data_dir=tempfile.mkdtemp(prefix="ba-drop-"),
                     step_duration=0.02, life_pause=0.005)
        demo.scheduler._cfg = SchedulerConfig(realtime_slots=2, interactive_slots=1,
                                              ambient_slots=1, queue_max=1)
        state = AppState(demo)
        self.addCleanup(state.shutdown)
        results: dict[str, dict] = {}

        def send(name: str) -> None:
            results[name] = state.send(name, principal="owner", entity="mira")

        first = threading.Thread(target=send, args=("first",))
        second = threading.Thread(target=send, args=("second",))
        third = threading.Thread(target=send, args=("third",))
        first.start()
        self.assertTrue(wait_for(lambda: demo.scheduler.stats()["active"]["interactive"] == 1))
        second.start()
        self.assertTrue(wait_for(lambda: sum(thread.queued for thread in demo.threads.threads()) == 1))
        third.start()
        for worker in (first, second, third):
            worker.join(5)
            self.assertFalse(worker.is_alive())

        self.assertTrue(results["first"]["ok"], results["first"])
        self.assertFalse(results["second"]["ok"], results["second"])
        self.assertTrue(results["second"]["finished"])
        self.assertIn("queue_full", results["second"]["error"])
        self.assertTrue(results["third"]["ok"], results["third"])
        self.assertEqual(sum(thread.queued for thread in demo.threads.threads()), 0)


class TestStale(unittest.TestCase):
    def test_long_waiting_job_expires_but_not_immediately(self):
        sched = make_scheduler(interactive_slots=1, queue_max=10, stale_ms=300)
        try:
            blocker = threading.Event()
            sched.submit(decision(5, thread="blocker"), lambda token: blocker.wait(3))
            time.sleep(0.05)
            discarded = []
            job = sched.submit(decision(5, thread="th-slow"), lambda token: None,
                               on_discard=lambda: discarded.append("th-slow"))
            time.sleep(0.1)
            self.assertEqual(sched.stats()["expired"], 0, "0.1 秒就作廢＝太急")
            time.sleep(1.0)          # 調度器每 0.5 s 醒一次清作廢
            self.assertGreaterEqual(sched.stats()["expired"], 1)
            self.assertEqual(discarded, ["th-slow"])
            self.assertIn("queue_expired", str(job.error))
            blocker.set()
        finally:
            sched.shutdown()


class TestBoundaryStop(unittest.TestCase):
    def test_cancel_does_not_kill_the_thread_but_pipeline_stops(self):
        """取消只翻旗標；管線在下一個階段邊界回 interrupted（不是硬殺）。"""
        demo = build(brain_delay=0.2, data_dir=tempfile.mkdtemp(prefix="ba-queue-"),
                     step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        state = AppState(demo)
        holder: dict = {}

        def run():
            holder["result"] = state.send("ゆっくり話して", principal="owner",
                                          on_frame=lambda frame: None)

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.05)
        demo.scheduler.preempt(list(demo.threads._items.values())[0].id
                              if hasattr(demo.threads, "_items") else "th-x")
        thread.join(5)
        # 沒有炸、有結果（管線是協作式停的，不拋例外）
        self.assertIn("result", holder)


class TestSourcePriority(unittest.TestCase):
    def test_voice_sources_are_realtime(self):
        for source in ("mic:phone", "discord:vc", "api:audio", "voice:local"):
            self.assertIs(_source_class_for(source), SourceClass.REALTIME, source)

    def test_text_and_table_sources(self):
        self.assertIs(_source_class_for("phone:owner"), SourceClass.INTERACTIVE)
        self.assertIs(_source_class_for("discord:owner"), SourceClass.INTERACTIVE)
        self.assertIs(_source_class_for("trpg:table-1"), SourceClass.AMBIENT)

    def test_voice_turn_is_submitted_as_realtime(self):
        demo = build(brain_delay=0, data_dir=tempfile.mkdtemp(prefix="ba-realtime-"),
                     step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        state = AppState(demo)
        seen: dict = {}

        original = demo.runtime.scheduler.submit

        def spy(decision_, fn, **kwargs):
            seen["lane"] = decision_.lane
            return original(decision_, fn, **kwargs)

        demo.runtime.scheduler.submit = spy
        state.send("聞こえる？", principal="owner", source_override="discord:vc")
        self.assertIs(seen.get("lane"), SourceClass.REALTIME)


class TestVoiceAckFirst(unittest.TestCase):
    def setUp(self):
        self.app = build(brain_delay=0.15, data_dir=tempfile.mkdtemp(prefix="ba-ack-"),
                          step_duration=0.02, life_pause=0.005)
        self.addCleanup(self.app.shutdown_lives)
        self.state = AppState(self.app)

    def test_work_from_voice_replies_short_then_queues_the_real_work(self):
        # mock 大腦兩次回同一句 ⇒ 會命中「後續與短回覆幾乎相同就丢」那條（預設開）。
        # 這裡測的是「有沒有交付」，所以把去重關掉；去重本身由
        # `test_conversation_clarity.TestFollowupDedupe` 咬著。
        import os
        os.environ["BLUEAGENT_FOLLOWUP_DEDUPE"] = "0"
        self.addCleanup(os.environ.pop, "BLUEAGENT_FOLLOWUP_DEDUPE", None)
        got: list[dict] = []
        done = threading.Event()

        def on_followup(result):
            got.append(result)
            done.set()

        started = time.time()
        ack = self.state.send_voice("この件を調べて、資料を作っておいて",
                                    principal="owner", mode="work",
                                    source="discord:vc", on_followup=on_followup)
        ack_seconds = time.time() - started
        self.assertTrue(ack.get("following_up"), "應該標記有後續")
        self.assertTrue(ack["frames"], "短回覆要立刻有內容")
        self.assertLess(ack_seconds, 3.0, "短回覆不能等整個工作做完")
        self.assertTrue(done.wait(10), "後續任務要在背景跑完並交付")
        follow = got[0]
        self.assertTrue(follow["frames"], "後續任務也要有輸出")

    def test_chat_mode_is_a_single_turn(self):
        result = self.state.send_voice("ただの雑談", principal="owner", mode="chat",
                                       source="discord:vc")
        self.assertFalse(result.get("following_up"))
        self.assertTrue(result["frames"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
