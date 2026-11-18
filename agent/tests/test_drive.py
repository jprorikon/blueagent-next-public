#!/usr/bin/env python3
"""內驅（`runtime/inner/drive.py`，owner 2026-11-09）。

判據：
  ① 只認**具體未結項**（交付／停住的任務／沒驗的假設），不是「時間到了就催」；
  ② 出手要記帳：世界沒變 ⇒ 空轉 ⇒ 冷卻**那一條**（2h→4h→8h）；有變 ⇒ productive；
  ③ 每日上限與最小間隔照樣擋；開不成（睡著／想休息）15 分內不重問；
  ④ 心跳在「心情不夠想開口」那一支才會走內驅，而且真的開得成一件事。

2026-11-09 評估後補的（第一版的測試自己寫 `done_at`，跟真資料對不上所以一直綠）：
  · 資料一律用**真的格式**造：交付走 `PendingDeliveryStore`（`cleared_at`／`expired_at`），
    任務用台帳的狀態（pending／processing／hanging／locked／completed）；
  · 結帳要等那一件做完（真大腦一步至少 60 秒，出手 90 秒就判空轉是錯的）；
  · 驗過的假設檔沒再改就不再撿；一條冷卻不擋同類別的另一條；
  · 生物鐘與「心情不好先休息」照生活輪換那一套擋；任務類是叫醒生活迴圈照台帳續，不另開一件。
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose import self_work_start as start_hook
from agent.brain.domain.activity import Activity, ActivityStatus
from agent.brain.runtime.activity_store import ActivityStore
from agent.brain.runtime.heartbeat import Heartbeat
from agent.brain.runtime.inner import drive
from agent.brain.runtime.inner.pending_delivery import PendingDeliveryStore
from agent.brain.runtime.life_loop import LifeLoop

NOW = 1_800_000_000.0
WHAT = "観測記録、ファイルで送って"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        for folder in ("tasks", "works", "pending_delivery", "drive"):
            (self.base / folder).mkdir(parents=True, exist_ok=True)

    def write(self, relative: str, body) -> None:
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body,
                        encoding="utf-8")

    def deliveries(self, at: float = NOW) -> PendingDeliveryStore:
        return PendingDeliveryStore(self.base / "pending_delivery", clock=lambda: at)

    def delivery(self, hours_ago: float = 3.0) -> dict:
        """走真的 store 記一筆（`cleared_at`／`expired_at` 都是 0.0，跟生產同形）。"""
        row = self.deliveries().note_request("aster", "owner", WHAT, at=NOW - hours_ago * 3600)
        self.assertIsNotNone(row, "這句要被認成交付請求")
        return row

    def task(self, state: str, *, hours_ago: float = 2.0, task_id: str = "task_1",
             title: str = "観測表を仕上げる") -> dict:
        """台帳（`inner/task_ledger.py`）的真欄位。"""
        return {"id": task_id, "title": title, "preset": "obs", "state": state, "priority": 0.5,
                "created_at": NOW - 9000, "updated_at": NOW - hours_ago * 3600, "cursor": 1,
                "activity_id": "", "attempts": 0, "note": "", "due_at": 0.0, "wait_until": 0.0}

    def tasks(self, *items: dict) -> None:
        self.write("tasks/aster.json", {"entity": "aster", "updated_at": NOW, "items": list(items)})

    def work(self, name: str, text: str, *, mtime: float = NOW - 7200) -> pathlib.Path:
        path = self.base / "works" / "aster" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path


class TestSignals(Base):
    def test_three_sources_are_found_with_their_own_age(self) -> None:
        self.delivery(3.0)
        self.tasks(self.task("hanging"))
        self.work("2026-11-09-観測表.md", "本文……この値は未検証。\n")
        rows = drive.signals(self.base, "aster", now=NOW)
        self.assertEqual(sorted(row["kind"] for row in rows), ["delivery", "task", "verify"])
        self.assertTrue(all(row["why"] for row in rows), "每一條都要有可讀的理由")
        self.assertEqual(rows[0]["kind"], "delivery", "最久、且是對人的承諾 ⇒ 排第一")

    def test_ledger_open_states_are_the_ledgers_own(self) -> None:
        """台帳的「未結」是 pending／processing／hanging；locked 是故意等著的，completed 是結了。"""
        self.tasks(*(self.task(state, task_id=f"t_{state}")
                     for state in ("pending", "processing", "hanging", "locked", "outdated",
                                   "completed")))
        refs = sorted(row["ref"] for row in drive.signals(self.base, "aster", now=NOW, limit=10))
        self.assertEqual(refs, ["t_hanging", "t_pending", "t_processing"])

    def test_fresh_items_are_not_leads(self) -> None:
        self.delivery(0.1)                       # 剛問的，別催
        self.tasks(self.task("processing", hours_ago=0.01))
        self.assertEqual(drive.signals(self.base, "aster", now=NOW), [])

    def test_delivered_and_expired_items_are_closed(self) -> None:
        """交了（`clear_delivered` ⇒ cleared_at）與過了 TTL（⇒ expired_at）都不再是欠帳。"""
        self.delivery(3.0)
        self.deliveries().clear_delivered("aster")
        self.assertEqual(drive.signals(self.base, "aster", now=NOW), [], "交了就結了")
        self.deliveries().note_request("aster", "owner", "写真、ファイルで送って", at=NOW - 4 * 86400)
        self.assertEqual(drive.signals(self.base, "aster", now=NOW), [], "過了 TTL 就不追了")

    def test_plain_word_hold_is_not_an_unverified_mark(self) -> None:
        self.work("memo.md", "この件は保留にして、次の話題へ。\n")
        self.assertEqual(drive.signals(self.base, "aster", now=NOW), [])


class TestAccounting(Base):
    def setUp(self) -> None:
        super().setUp()
        self.delivery(3.0)

    def nudge(self, at: float = NOW, *, activity_id: str = "act_1") -> dict:
        row = drive.signals(self.base, "aster", now=at)[0]
        drive.note_nudge(self.base, "aster", row, drive.fingerprint(self.base, "aster", now=at), now=at,
                         activity_id=activity_id)
        return row

    def test_delivering_changes_the_fingerprint(self) -> None:
        before = drive.fingerprint(self.base, "aster", now=NOW)
        self.deliveries().clear_delivered("aster")
        self.assertNotEqual(drive.fingerprint(self.base, "aster", now=NOW), before, "交出去 ⇒ 世界變了")

    def test_settle_waits_until_that_activity_is_done(self) -> None:
        self.nudge()
        busy = {"act_1": True}
        running = lambda entity, activity_id: busy.get(activity_id, False)   # noqa: E731
        fp = drive.fingerprint(self.base, "aster", now=NOW + 600)
        self.assertIsNone(drive.settle(self.base, "aster", fp, now=NOW + 600, running=running),
                          "那一件還在做 ⇒ 不結帳（出手 90 秒就判空轉是錯的）")
        self.assertIsNotNone(drive.load(self.base, "aster")["pending"])
        self.deliveries().clear_delivered("aster")                 # 做的途中真的交了
        busy["act_1"] = False
        settled = drive.settle(self.base, "aster", drive.fingerprint(self.base, "aster", now=NOW + 900),
                               now=NOW + 900, running=running)
        self.assertEqual((settled["kind"], settled["changed"]), ("delivery", True))
        self.assertEqual(drive.load(self.base, "aster")["stats"]["productive"], 1)

    def test_settle_gives_up_waiting_after_the_cap(self) -> None:
        self.nudge()
        settled = drive.settle(self.base, "aster", drive.fingerprint(self.base, "aster", now=NOW),
                               now=NOW + drive.SETTLE_MAX_S + 1, running=lambda *_: True)
        self.assertIsNotNone(settled, "卡死也要結帳，不能永遠掛著")
        self.assertFalse(settled["changed"])

    def test_spin_cools_that_item_only(self) -> None:
        row = self.nudge()
        settled = drive.settle(self.base, "aster", drive.fingerprint(self.base, "aster", now=NOW + 120),
                               now=NOW + 120)
        self.assertEqual((settled["kind"], settled["changed"]), ("delivery", False))
        state = drive.load(self.base, "aster")
        self.assertGreater(state["refs"][f"delivery:{row['ref']}"]["until"], NOW + 120)
        later = NOW + 1000
        self.assertIsNone(drive.pick(self.base, "aster", drive.signals(self.base, "aster", now=later),
                                     now=later + drive.RETRY_S, gap_s=0), "冷卻中的那一條不再選")
        self.deliveries(NOW - 2 * 3600).note_request("aster", "owner", "写真、ファイルで送って",
                                                     at=NOW - 2 * 3600)
        other = drive.pick(self.base, "aster", drive.signals(self.base, "aster", now=later),
                           now=later + drive.RETRY_S, gap_s=0)
        self.assertIsNotNone(other, "同類別的另一條不受牽連")
        self.assertNotEqual(other["ref"], row["ref"])

    def test_cooldown_steps_up_per_item(self) -> None:
        row = self.nudge()
        key = f"delivery:{row['ref']}"
        drive.settle(self.base, "aster", drive.fingerprint(self.base, "aster", now=NOW + 120),
                     now=NOW + 120)
        first = drive.load(self.base, "aster")["refs"][key]["until"] - (NOW + 120)
        drive.note_nudge(self.base, "aster", row, drive.fingerprint(self.base, "aster", now=NOW + 9000),
                         now=NOW + 9000)
        drive.settle(self.base, "aster", drive.fingerprint(self.base, "aster", now=NOW + 9120),
                     now=NOW + 9120)
        second = drive.load(self.base, "aster")["refs"][key]["until"] - (NOW + 9120)
        self.assertEqual((first, second), drive.COOLDOWN_STEPS[:2])

    def test_daily_cap_gap_and_pending(self) -> None:
        rows = drive.signals(self.base, "aster", now=NOW)
        drive.note_nudge(self.base, "aster", rows[0], "x", now=NOW)
        self.assertIsNone(drive.pick(self.base, "aster", rows, now=NOW + 10000, gap_s=0),
                          "上一筆沒結帳 ⇒ 不再出手")
        drive.settle(self.base, "aster", "y", now=NOW + 120)        # 有變 ⇒ 結了、不冷卻
        self.assertIsNone(drive.pick(self.base, "aster", rows, now=NOW + 600, gap_s=1800),
                          "間隔不到不再出手")
        self.assertIsNotNone(drive.pick(self.base, "aster", rows, now=NOW + 10000, gap_s=1800))
        state = drive.load(self.base, "aster")
        state["count_today"] = 4
        drive.save(self.base, "aster", state)
        self.assertIsNone(drive.pick(self.base, "aster", rows, now=NOW + 20000, gap_s=0,
                                     daily_cap=4), "每日上限")


class TestVerifyResolution(Base):
    def test_verified_file_is_not_picked_again_until_it_changes(self) -> None:
        path = self.work("観測表.md", "この値は未検証。\n")
        row = drive.signals(self.base, "aster", now=NOW)[0]
        drive.note_nudge(self.base, "aster", row, drive.fingerprint(self.base, "aster", now=NOW), now=NOW)
        (self.base / "notes" / "aster").mkdir(parents=True)
        (self.base / "notes" / "aster" / "check.md").write_text("確かめた：合っていた", encoding="utf-8")
        settled = drive.settle(self.base, "aster", drive.fingerprint(self.base, "aster", now=NOW + 300),
                               now=NOW + 300)
        self.assertTrue(settled["changed"], "寫了筆記 ⇒ 有進展")
        later = NOW + 3 * 86400
        rows = drive.signals(self.base, "aster", now=later)
        self.assertIsNone(drive.pick(self.base, "aster", rows, now=later, gap_s=0),
                          "驗過、檔案沒改 ⇒ 不再撿同一個「未検証」")
        os.utime(path, (later - 60, later - 60))                  # 她之後又改了這份
        rows = drive.signals(self.base, "aster", now=later)
        self.assertIsNotNone(drive.pick(self.base, "aster", rows, now=later, gap_s=0))


class TestAttempt(Base):
    def test_opens_at_most_one_work_per_attempt(self) -> None:
        self.delivery(3.0)
        opened = []
        ok = drive.attempt("aster", self.base, NOW + 3600,
                           on_work=lambda e, s: opened.append(s) or "act_1")
        self.assertTrue(ok)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["kind"], "delivery")
        self.assertEqual(drive.load(self.base, "aster")["pending"]["activity_id"], "act_1")
        again = drive.attempt("aster", self.base, NOW + 3600 + 60, running=lambda *_: True,
                              on_work=lambda e, s: opened.append(s) or "act_2")
        self.assertFalse(again, "同一筆還沒結帳 ⇒ 不重複出手")
        self.assertEqual(len(opened), 1)

    def test_declined_work_backs_off(self) -> None:
        """開不成（睡著／想休息）⇒ 15 分內不再問，不是每個心跳都去敲。"""
        self.delivery(3.0)
        calls = []
        decline = lambda e, s: calls.append(s) and None   # noqa: E731
        self.assertFalse(drive.attempt("aster", self.base, NOW, on_work=decline))
        self.assertFalse(drive.attempt("aster", self.base, NOW + 60, on_work=decline))
        self.assertEqual(len(calls), 1)
        drive.attempt("aster", self.base, NOW + drive.RETRY_S + 1, on_work=decline)
        self.assertEqual(len(calls), 2)

    def test_fixed_hours_only_when_asked(self) -> None:
        self.delivery(3.0)
        night = time.mktime((2026, 11, 9, 3, 0, 0, 0, 0, -1))
        opened = []
        self.assertFalse(drive.attempt("aster", self.base, night, hours=(7, 23),
                                       on_work=lambda e, s: opened.append(s) or True))
        self.assertEqual(opened, [])

    def test_no_budget_no_action(self) -> None:
        self.delivery(3.0)

        class Poor:
            def allows(self, entity, cost=1.0):
                return False

        opened = []
        self.assertFalse(drive.attempt("aster", self.base, NOW + 3600, budget=Poor(),
                                       on_work=lambda e, s: opened.append(s) or True))
        self.assertEqual(opened, [])


class FakeLoop:
    def __init__(self, *, status: str = "", awake: bool = True) -> None:
        self.status = status
        self.awake = awake
        self.started: list = []
        self.woken = 0
        self._circadian = object()
        self.current_id = "prev"

    def snapshot(self) -> dict:
        return {"activity": {"id": self.current_id, "status": self.status} if self.status else None}

    def can_open_new(self, now=None) -> bool:
        return self.awake

    def start(self, activity) -> None:
        self.started.append(activity)
        self.current_id = activity.id
        self.status = "running"

    def wake_now(self) -> bool:
        self.woken += 1
        return True


class Mood:
    def __init__(self, label: str) -> None:
        self.label = label

    def mood(self, entity, at=None):
        return {"valence": -0.2, "arousal": 0.05, "social_need": 0.0, "propensity": 0.1,
                "label": self.label}


class TestStartDrive(Base):
    SIGNAL = {"kind": "delivery", "ref": "dl_1", "title": "溜まっている交付を一つ片付ける",
              "steps": ["材料を確かめる", "file_send で送る"], "why": "…"}

    def test_opens_a_drive_activity_and_reports_its_id(self) -> None:
        loop = FakeLoop()
        started = start_hook.start_drive("aster", self.SIGNAL, lives={"aster": loop})
        self.assertEqual(started, loop.started[0].id)
        self.assertTrue(start_hook.drive_running("aster", started, lives={"aster": loop}))
        loop.status = "done"
        self.assertFalse(start_hook.drive_running("aster", started, lives={"aster": loop}))

    def test_asleep_or_winding_does_not_open(self) -> None:
        loop = FakeLoop(awake=False)
        self.assertIsNone(start_hook.start_drive("aster", self.SIGNAL, lives={"aster": loop}))
        self.assertEqual(loop.started, [])

    def test_bad_mood_rests_like_the_rotation_does(self) -> None:
        from unittest import mock

        from agent.brain.runtime.inner import willing
        loop = FakeLoop()
        with mock.patch.object(willing, "roll", return_value=0.0):
            rest = start_hook.start_drive("aster", self.SIGNAL, lives={"aster": loop},
                                          affect=Mood("しょんぼり"))
        self.assertIsNone(rest, "心情不好可以先不做（owner 2026-11-09）——內驅也一樣")
        self.assertEqual(loop.started, [])

    def test_busy_loop_is_not_interrupted(self) -> None:
        loop = FakeLoop(status="running")
        self.assertIsNone(start_hook.start_drive("aster", self.SIGNAL, lives={"aster": loop}))

    def test_task_signal_wakes_the_loop_instead_of_duplicating(self) -> None:
        loop = FakeLoop()
        signal = {"kind": "task", "ref": "task_1", "why": "「観測表」停在 hanging", "steps": ["…"]}
        self.assertIs(start_hook.start_drive("aster", signal, lives={"aster": loop}), True)
        self.assertEqual((loop.woken, loop.started), (1, []), "台帳那件照常續，不另開一件")


class TestLifeLoopHooks(Base):
    def test_wake_now_only_when_idle(self) -> None:
        store = ActivityStore(self.base / "activities")
        loop = LifeLoop("aster", store, lambda activity, step: "ok", step_pause=0.01,
                        clock=lambda: NOW, next_activity=lambda entity, *_: None)
        try:
            loop._resume_at = NOW + 1800
            self.assertTrue(loop.wake_now())
            self.assertEqual(loop._resume_at, NOW, "不等休息到點")
            running = Activity.create("aster", "做到一半", ["一", "二"])
            running.status = ActivityStatus.RUNNING
            loop._activity = running
            self.assertFalse(loop.wake_now(), "手上有事不叫")
        finally:
            loop.shutdown()

    def test_can_open_new_follows_the_circadian_clock(self) -> None:
        class Clock:
            def __init__(self, asleep: bool, settling: bool) -> None:
                self.flags = (asleep, settling)

            def asleep(self, now=None):
                return self.flags[0]

            def settling_in(self, now=None):
                return self.flags[1]

            def snapshot(self, now=None):
                return {"sleep": "01:30", "wake": "08:30"}

            def settle(self, now=None):
                return None

        store = ActivityStore(self.base / "activities-c")
        for flags, expected in (((False, False), True), ((True, False), False),
                                ((False, True), False)):
            loop = LifeLoop("aster", store, lambda activity, step: "ok", step_pause=0.01,
                            clock=lambda: NOW, circadian=Clock(*flags))
            try:
                self.assertEqual(loop.can_open_new(NOW), expected, flags)
            finally:
                loop.shutdown()


class TestHeartbeatUsesDrive(Base):
    def test_below_threshold_turns_into_drive_work(self) -> None:
        day = time.mktime((2026, 11, 9, 12, 0, 0, 0, 0, -1))
        self.deliveries(day).note_request("aster", "owner", WHAT, at=day - 3 * 3600)
        opened = []

        class Calm:
            def advance(self, entity):
                return {}

            def mood(self, entity, at=None):
                return {"valence": 0.0, "arousal": 0.0, "social_need": 0.0, "propensity": 0.05,
                        "label": "ふつう"}

            def idle_s(self, entity, at=None):
                return 0.0

        beat = Heartbeat(entity="aster", affect=Calm(), gate=object(), interval_s=999,
                         clock=lambda: day, threshold=0.5,
                         drive_base=str(self.base),
                         on_drive_work=lambda entity, signal: opened.append(signal) or "act_1",
                         drive_running=lambda entity, activity_id: True)
        self.assertEqual(beat.tick(at=day), [], "內驅不說話，只做事")
        self.assertEqual(beat.last_skip_reason, "drive")
        self.assertEqual(len(opened), 1)
        beat.tick(at=day + 600)
        self.assertEqual(len(opened), 1, "那一件還在做 ⇒ 不結帳、不重複開")
        self.assertIsNotNone(drive.load(self.base, "aster")["pending"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
