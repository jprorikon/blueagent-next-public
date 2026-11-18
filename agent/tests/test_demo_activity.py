#!/usr/bin/env python3
"""Demo：活动的串行/断点/恢复"""

from __future__ import annotations

from demo_common import *  # noqa: F401,F403


class TestActivityLoop(unittest.TestCase):
    def test_serial_and_each_step_checkpointed(self):
        store = ActivityStore(tempfile.mkdtemp(prefix="ba-life-"))
        counter = {"now": 0, "max": 0}
        lock = threading.Lock()

        def step_fn(activity, step):
            with lock:
                counter["now"] += 1
                counter["max"] = max(counter["max"], counter["now"])
            time.sleep(0.03)
            with lock:
                counter["now"] -= 1
            return "ok"

        loop = LifeLoop("mira", store, step_fn, step_pause=0.005)
        try:
            loop.start(Activity.create("mira", "串行测试", ["一", "二", "三"]))
            self.assertTrue(wait_for(lambda: loop.snapshot()["activity"]["status"] == "done"))
            snapshot = loop.snapshot()["activity"]
            self.assertEqual(snapshot["cursor"], 3)
            self.assertEqual(counter["max"], 1)
            self.assertGreater(snapshot["checkpoint_at"], 0)
            loaded = store.load("mira")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.cursor, 3)
            self.assertTrue(all(step.status is StepStatus.DONE for step in loaded.steps))
        finally:
            loop.shutdown()

    def test_pause_writes_checkpoint_and_resume_continues(self):
        store = ActivityStore(tempfile.mkdtemp(prefix="ba-life-"))

        def step_fn(activity, step):
            time.sleep(0.04)
            return "ok"

        loop = LifeLoop("mira", store, step_fn, step_pause=0.005)
        try:
            loop.start(Activity.create("mira", "暂停测试", ["一", "二", "三"]))
            self.assertTrue(wait_for(lambda: loop.snapshot()["activity"]["cursor"] >= 1))
            loop.pause("manual")
            self.assertTrue(wait_for(lambda: loop.snapshot()["activity"]["status"] == "paused"))
            paused = store.load("mira")
            self.assertIsNotNone(paused)
            self.assertEqual(paused.status.value, "paused")
            before = paused.cursor
            loop.resume()
            self.assertTrue(wait_for(lambda: loop.snapshot()["activity"]["status"] == "done"))
            self.assertGreater(loop.snapshot()["activity"]["cursor"], before)
        finally:
            loop.shutdown()

    def test_crash_recovery_downgrades_running_step(self):
        store = ActivityStore(tempfile.mkdtemp(prefix="ba-life-"))
        activity = Activity.create("mira", "崩溃恢复", ["一", "二"])
        activity.status = __import__("agent.brain.domain.activity", fromlist=["ActivityStatus"]).ActivityStatus.RUNNING
        activity.steps[0].status = StepStatus.RUNNING
        store.save(activity)

        loop = LifeLoop("mira", store, lambda activity, step: "ok", step_pause=0.005)
        try:
            snapshot = loop.snapshot()
            self.assertTrue(snapshot["recovered"])
            self.assertEqual(snapshot["activity"]["status"], "paused")
            self.assertEqual(snapshot["activity"]["steps"][0]["status"], "pending")
        finally:
            loop.shutdown()

    def test_human_interrupt_pauses_then_auto_resumes(self):
        store = ActivityStore(tempfile.mkdtemp(prefix="ba-life-"))
        entered = threading.Event()

        def step_fn(activity, step):
            entered.set()
            time.sleep(0.04)
            return "ok"

        # resume_beats=0：只驗「停下來、人走後自己接著做」；2026-11-03 起預設要沉 3 拍
        #（每拍 ≥30 s）才續做，那段延遲由 test_life_checkpoint 驗
        loop = LifeLoop("mira", store, step_fn, step_pause=0.005, resume_beats=0)
        try:
            loop.start(Activity.create("mira", "打断测试", ["一", "二", "三"]))
            self.assertTrue(entered.wait(2.0))
            loop.human_enter()
            self.assertTrue(wait_for(lambda: loop.snapshot()["activity"]["status"] == "paused"))
            loop.human_exit()
            self.assertTrue(wait_for(lambda: loop.snapshot()["activity"]["status"] == "done"))
        finally:
            loop.shutdown()

    def test_demo_activity_survives_new_state(self):
        root = tempfile.mkdtemp(prefix="ba-life-")
        # 每一步留出明确窗口，避免快机器在 pause 前已经跑完整件活动。
        demo = build(brain_delay=0, data_dir=root, step_duration=0.2, life_pause=0.005)
        demo.principals.register(Principal("owner", "主人", owner=True), replace=True)
        state = AppState(demo)
        state.start_activity()
        self.assertTrue(wait_for(lambda: (state.snapshot()["life"]["activity"] or {}).get("cursor", 0) >= 1))
        state.pause_activity()
        snapshot = state.snapshot()["life"]
        cursor = snapshot["activity"]["cursor"]
        demo.life.shutdown()

        # 新進程：從同一個 data_dir 重建，應停在同一個 cursor，不會從頭再來。
        again = build(brain_delay=0, data_dir=root, step_duration=0.2, life_pause=0.005)
        again.principals.register(Principal("owner", "主人", owner=True), replace=True)
        restored = again.life.snapshot()
        self.assertGreaterEqual(restored["activity"]["cursor"], cursor)
        self.assertEqual(restored["activity"]["status"], "paused")
        again.life.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
