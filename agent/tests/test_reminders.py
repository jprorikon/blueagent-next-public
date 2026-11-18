#!/usr/bin/env python3
"""「幾跳之後提某件事」：預約存放（`runtime/inner/reminders.py`）、工具、心跳取用。

owner 2026-10-27：「讓她明確知道多少跳之後又有什么事」。
"""

from __future__ import annotations

import tempfile
import time
import unittest

from agent.brain.runtime.heartbeat import Heartbeat
from agent.brain.runtime.inner.reminders import ReminderStore
from agent.brain.runtime.tools.base import ToolContext
from agent.brain.runtime.tools.remind import RemindTool


class TestReminderStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ReminderStore(self.tmp.name)

    def test_ticks_roundtrip_and_fire_once(self):
        self.store.add("mira", "問他晚饭吃了没", tick=10, after_ticks=5)
        pending = self.store.pending("mira", tick=10)
        self.assertEqual([row["text"] for row in pending], ["問他晚饭吃了没"])
        self.assertEqual(pending[0]["at_tick"], 15)
        self.assertEqual(self.store.take_due("mira", tick=14), [], "還沒到")
        due = self.store.take_due("mira", tick=15)
        self.assertEqual([row["text"] for row in due], ["問他晚饭吃了没"])
        self.assertEqual(self.store.take_due("mira", tick=99), [], "只講一次")

    def test_minutes_keep_an_estimated_tick_for_display_only(self):
        record = self.store.add("mira", "看天氣", tick=3, after_minutes=10, interval_s=30.0)
        self.assertEqual(record["est_tick"], 3 + 20)     # 600s / 30s（只拿來顯示）
        self.assertEqual(record["at_tick"], 0, "分鐘預約不靠跳號觸發")
        self.assertTrue(record["at"] > time.time())

    def test_minutes_do_not_fire_early_when_beats_speed_up(self):
        """★ review 2026-10-28 #3：建立時 75 秒一跳 ⇒ 10 分鐘≈8 跳；之後變 30 秒一跳，
        第 8 跳只過了 240 秒——以前跳號先到就響，提前 6 分鐘。"""
        self.store.add("mira", "看天氣", tick=0, after_minutes=10, interval_s=75.0, now=0.0)
        self.assertEqual(self.store.due("mira", tick=8, now=240.0), [])
        self.assertEqual(len(self.store.due("mira", tick=8, now=600.0)), 1)

    def test_legacy_minute_rows_with_both_fields_follow_the_clock(self):
        """舊檔裡分鐘預約 `at_tick` 與 `at` 都寫了：有時刻就只看時刻。"""
        self.store._write("mira", [{"text": "舊", "created_at": 0.0, "at_tick": 8,
                                       "at": 600.0, "fired_at": 0.0}])
        self.assertEqual(self.store.due("mira", tick=8, now=240.0), [])

    def test_absolute_time_is_parsed_to_next_occurrence(self):
        now = time.mktime((2026, 10, 27, 12, 0, 0, 0, 0, -1))
        record = self.store.add("mira", "說晚安", at_time="23:30", now=now)
        self.assertAlmostEqual(record["at"], now + 11.5 * 3600, delta=2)
        self.assertEqual(record["at_tick"], 0)

    def test_bad_time_is_rejected_and_not_stored(self):
        """★ review 2026-10-28 #4：以前存成 at=0／at_tick=0 ⇒ 永遠不到期，工具卻回成功。"""
        with self.assertRaises(ValueError):
            self.store.add("mira", "x", at_time="25:99", now=time.time())
        self.assertEqual(self.store.items("mira"), [])

    def test_concurrent_add_and_mark_do_not_lose_rows(self):
        """回話執行緒在加、心跳執行緒在標 fired：整段讀改寫要鎖住，不能互相蓋掉。"""
        import threading
        self.store.add("mira", "seed", tick=0, after_ticks=1, now=1.0)
        seed = self.store.due("mira", tick=5, now=2.0)
        start = threading.Barrier(2)

        def adder():
            start.wait()
            for index in range(40):
                self.store.add("mira", f"n{index}", tick=0, after_ticks=1000, now=10.0 + index)

        def marker():
            start.wait()
            for _ in range(40):
                self.store.mark_fired("mira", seed, now=3.0)

        threads = [threading.Thread(target=adder), threading.Thread(target=marker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        rows = self.store.items("mira")
        self.assertEqual(len(rows), 41)
        self.assertTrue(rows[0]["fired_at"], "標過的不能被加的那一邊蓋回去")


class TestRemindTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ReminderStore(self.tmp.name)
        self.tool = RemindTool(self.store, beats=lambda entity: {"ticks": 100, "interval_s": 30.0})
        self.ctx = ToolContext(entity="mira", principal="owner", thread_id="th-1",
                               source="app", voice=None)

    def test_schedules_relative_to_current_tick(self):
        result = self.tool.run(self.ctx, {"note": "問他晚飯", "after_ticks": 20})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["at_tick"], 120)
        # ★ 相對拍數（20），不是絕對跳號（以前寫成「120 跳後」）
        self.assertIn("20 拍あと", result.data["when"])
        self.assertNotIn("120 拍あと", result.data["when"])
        self.assertIn("問他晚飯", result.data["summary"])

    def test_clock_time_is_readable(self):
        result = self.tool.run(self.ctx, {"note": "おやすみ", "at": "23:30"})
        self.assertTrue(result.ok, result.error)
        self.assertIn("23:30", result.data["when"])
        self.assertNotRegex(result.data["when"], r"\d{9,}", "不要 unix 秒")

    def test_bad_time_is_a_tool_error(self):
        result = self.tool.run(self.ctx, {"note": "おやすみ", "at": "25:99"})
        self.assertFalse(result.ok)
        self.assertIn("bad_arguments", result.error)
        self.assertEqual(self.store.items("mira"), [])

    def test_minutes_show_an_estimate(self):
        result = self.tool.run(self.ctx, {"note": "天気", "after_minutes": 10})
        self.assertTrue(result.ok, result.error)
        self.assertIn("およそ 20 拍あと", result.data["when"])     # 600s / 30s

    def test_note_is_required(self):
        self.assertFalse(self.tool.run(self.ctx, {}).ok)

    def test_too_many_pending_is_refused(self):
        for index in range(30):
            self.store.add("mira", f"m{index}", tick=0, after_ticks=1000 + index)
        result = self.tool.run(self.ctx, {"note": "再一筆", "after_ticks": 5})
        self.assertFalse(result.ok)
        self.assertIn("too_many_reminders", result.error)


class FakeAffect:
    def advance(self, entity):
        return {}

    def mood(self, entity, *, at=None):
        return {"label": "ふつう", "propensity": 0.9, "social_need": 0.1, "arousal": 0.5}


class FakeGate:
    def allows(self, entity, *, now, principal):
        return True, "ok"

    def maybe_send(self, entity, text, *, principal, publish, now, urgency="", force=False):
        return {"sent": True, "frame": {"text": text}}


class TestHeartbeatTakesDue(unittest.TestCase):
    def test_due_reminder_reaches_compose_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReminderStore(tmp)
            store.add("mira", "問他晚飯", tick=0, after_ticks=2)
            seen: list[list[str]] = []

            def compose(entity, mood, due=()):
                seen.append(list(due))
                return "嗯……晚飯吃了嗎？"

            beat = Heartbeat(entity="mira", affect=FakeAffect(), gate=FakeGate(),
                             compose=compose, interval_s=30.0, reminders=store)
            beat.tick()
            beat.tick()
            self.assertEqual(seen[0], [])
            self.assertEqual(seen[1], ["問他晚飯"], "第 2 跳到期")
            beat.tick()
            self.assertEqual(seen[2], [], "只提一次")

    def test_due_reminder_survives_a_silent_tick(self):
        """★ 這一跳不想說話（低於門檻）⇒ 預約**留著**，下一次開口時才提；以前先標再判斷，直接作廢。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = ReminderStore(tmp)
            store.add("mira", "問他晚飯", tick=0, after_ticks=1)
            affect = FakeAffect()
            quiet = {"label": "ふつう", "propensity": 0.0, "social_need": 0.1, "arousal": 0.5}
            loud = affect.mood("mira")
            current = [quiet]
            affect.mood = lambda entity, *, at=None: current[0]
            seen: list[list[str]] = []

            def compose(entity, mood, due=()):
                seen.append(list(due))
                return "ねえ"

            beat = Heartbeat(entity="mira", affect=affect, gate=FakeGate(),
                             compose=compose, interval_s=30.0, reminders=store)
            self.assertEqual(beat.tick(), [])
            self.assertEqual(beat.last_skip_reason, "below_threshold")
            self.assertEqual([row["text"] for row in store.due("mira", tick=99)], ["問他晚飯"])
            current[0] = loud
            beat.tick()
            self.assertEqual(seen, [["問他晚飯"]])
            self.assertEqual(store.due("mira", tick=99), [], "講出口之後才算用掉")
            self.assertEqual(beat.last_spoken_tick, 2, "時間感的「距上次開口」靠它")

    def test_blocked_by_gate_keeps_the_reminder(self):
        class Blocked(FakeGate):
            def allows(self, entity, *, now, principal):
                return False, "quiet_hours"

        with tempfile.TemporaryDirectory() as tmp:
            store = ReminderStore(tmp)
            store.add("mira", "問他晚飯", tick=0, after_ticks=1)
            beat = Heartbeat(entity="mira", affect=FakeAffect(), gate=Blocked(),
                             compose=lambda entity, mood, due=(): "ねえ",
                             interval_s=30.0, reminders=store)
            beat.tick()
            self.assertEqual(len(store.due("mira", tick=99)), 1)



class TestOnlySpokenRemindersAreUsed(unittest.TestCase):
    """review 2026-10-28 #2：模板只說第一件，卻把同一跳到期的全部標掉。"""

    def _store(self, tmp, count):
        store = ReminderStore(tmp)
        for index in range(count):
            store.add("mira", f"件{index + 1}", tick=0, after_ticks=1, now=float(index))
        return store

    def _sent(self):
        texts = []

        class Gate(FakeGate):
            def maybe_send(self, entity, text, **kwargs):
                texts.append(text)
                return {"sent": True, "frame": {"text": text}}

        return Gate(), texts

    def test_template_says_every_due_reminder_it_marks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, 2)
            gate, texts = self._sent()
            Heartbeat(entity="mira", affect=FakeAffect(), gate=gate, interval_s=30.0,
                      reminders=store).tick()
            self.assertIn("件1", texts[0])
            self.assertIn("件2", texts[0])
            self.assertEqual(store.due("mira", tick=99), [])

    def test_template_overflow_stays_for_next_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, 4)
            gate, texts = self._sent()
            Heartbeat(entity="mira", affect=FakeAffect(), gate=gate, interval_s=30.0,
                      reminders=store).tick()
            self.assertNotIn("件4", texts[0])
            self.assertEqual([row["text"] for row in store.due("mira", tick=99)], ["件4"],
                             "沒塞進台詞的不能被標掉")

    def test_compose_failure_falls_back_to_template_with_all_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, 2)
            gate, texts = self._sent()

            def broken(entity, mood, due=()):
                raise RuntimeError("model down")

            Heartbeat(entity="mira", affect=FakeAffect(), gate=gate, compose=broken,
                      interval_s=30.0, reminders=store).tick()
            self.assertIn("件2", texts[0])
            self.assertEqual(store.due("mira", tick=99), [])

    def test_legacy_compose_without_due_consumes_nothing(self):
        """舊簽名 `compose(entity, mood)` 根本沒收到預約 ⇒ 一筆都不算講過。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, 1)
            gate, _texts = self._sent()
            Heartbeat(entity="mira", affect=FakeAffect(), gate=gate,
                      compose=lambda entity, mood: "やあ", interval_s=30.0,
                      reminders=store).tick()
            self.assertEqual(len(store.due("mira", tick=99)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
