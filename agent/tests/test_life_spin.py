#!/usr/bin/env python3
"""自我活動不自旋（owner 2026-11-09 做 1／2／3／4／6）＋ 心情不好可以先不做 ＋ 睡著時私聊先放著。

判據：
  ① 閉環（讀自己／同伴產出）的活動：上次之後要有一手材料才排得進去；閉環產出不算材料。
  ② 同一套的產出跟最近 3 次太像 ⇒ 冷卻 1→2→4 天（上限 7）；不像就歸零。
  ③ 一天裡閉環最多 1/3。
  ④ 預設裡沒有寫死的私人情緒；「又寫了一樣的」由真實結果記成私人的一句。
  ⑥ 她自己加任務要寫「きっかけ」、一天最多 2 件。
  ＋ 心情不好 ⇒ 這次先不開新活動（穩定擲骰、照實記原因）；手上空著的生活迴圈會休息到點再問。
  ＋ 睡著時私聊先放著；醒了每個會話開一輪；15 分鐘內同一人 3 則 ⇒ 被叫醒。
"""

from __future__ import annotations

import pathlib
import tempfile
import time
import unittest
from types import SimpleNamespace as NS

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.inner import spin
from agent.brain.runtime.life import PRESETS, Rotation, presets_for

DAY = time.mktime((2026, 11, 9, 9, 0, 0, 0, 0, -1))


def preset(key, inputs):
    return NS(key=key, inputs=inputs)


class TestSpinRules(unittest.TestCase):
    def test_inputs_are_tagged(self) -> None:
        kinds = {item.key: item.inputs for item in presets_for("aster")}
        self.assertEqual(kinds["observe-peers"], "peers")
        self.assertEqual(kinds["notes-tidy"], "own")
        self.assertEqual(kinds["method"], "external")
        self.assertEqual(kinds["skills-tidy"], "maintenance")

    def test_every_library_can_reach_outside(self) -> None:
        """防自旋的門檻會擋住閉環；庫裡讀外面的太少，她就幾乎沒事可做（アスター 以前只有 method 一套）。"""
        for who in ("mira", "aster", "nori"):
            library = [item for item in presets_for(who) if not getattr(item, "once_per_day", False)]
            outward = [item.key for item in library if item.inputs in spin.FIRST_ORDER]
            self.assertGreaterEqual(len(outward) * 2, len(library) - 1,
                                    f"{who}: 讀外面／創作的不到一半 {outward}")
        aster = {item.key: item.inputs for item in presets_for("aster")}
        for key in ("sky-tonight", "design-case", "obs-protocol", "bento-calc", "robot-parts"):
            self.assertEqual(aster.get(key), "external", key)

    def test_fresh_material(self) -> None:
        kinds = {"aster": {"notes-tidy": "own", "method": "external", "machine-log": "own"},
                 "nori": {"game-shop": "external", "review-quiz": "own"}}
        tidy = preset("notes-tidy", "own")
        picks = {"aster": [{"key": "notes-tidy", "at": 100.0}, {"key": "machine-log", "at": 200.0}]}
        self.assertFalse(spin.has_fresh_material("aster", tidy, since=100.0, picks=picks, kinds=kinds),
                         "閉環產出（machine-log）不算新材料")
        self.assertTrue(spin.has_fresh_material("aster", tidy, since=100.0, picks=picks, kinds=kinds,
                                                heard_at=150.0), "主人說過話 ⇒ 有材料")
        picks["aster"].append({"key": "method", "at": 300.0})
        self.assertTrue(spin.has_fresh_material("aster", tidy, since=100.0, picks=picks, kinds=kinds))
        self.assertTrue(spin.has_fresh_material("aster", tidy, since=0.0, picks={}, kinds=kinds),
                        "第一次總要讓她做一次")
        peers = preset("observe-peers", "peers")
        picks = {"aster": [], "nori": [{"key": "review-quiz", "at": 500.0}]}
        self.assertFalse(spin.has_fresh_material("aster", peers, since=400.0, picks=picks, kinds=kinds),
                         "同伴的閉環產出不算")
        picks["nori"].append({"key": "game-shop", "at": 600.0})
        self.assertTrue(spin.has_fresh_material("aster", peers, since=400.0, picks=picks, kinds=kinds))

    def test_quota(self) -> None:
        kinds = {"a": "own", "b": "external"}
        self.assertFalse(spin.closed_quota_ok([], kinds), "第一件不能就是閉環")
        self.assertTrue(spin.closed_quota_ok(["b", "b"], kinds))
        self.assertFalse(spin.closed_quota_ok(["b", "b", "a"], kinds))
        self.assertTrue(spin.closed_quota_ok(["b", "b", "a", "b", "b"], kinds))

    def test_novelty_backoff(self) -> None:
        state = spin.note_output({}, "判定を三つ表にまとめた：隠密、交渉、追跡", now=0.0)
        self.assertEqual(state["stale"], 0)
        state = spin.note_output(state, "判定を三つ表にまとめた：隠密、交渉、追跡。", now=0.0)
        self.assertEqual((state["stale"], state["cooldown_until"]), (1, 86400.0))
        state = spin.note_output(state, "判定を三つ表にまとめた：隠密、交渉、追跡！", now=0.0)
        self.assertEqual(state["cooldown_until"], 2 * 86400.0, "1 → 2 → 4 天")
        state = spin.note_output(state, "雨の日の野外探索で使う天候表を新しく作った", now=0.0)
        self.assertEqual((state["stale"], state["cooldown_until"]), (0, 0.0), "寫出不一樣的就歸零")


class TestRotationGates(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-spin-"))

    def test_closed_waits_for_material_then_runs(self) -> None:
        rotation = Rotation(self.root / "rot.json", heard=lambda who: 0.0)
        library = tuple(item for item in presets_for("aster") if not getattr(item, "once_per_day", False))
        kinds = {item.key: item.inputs for item in library}
        rotation._data = {"aster": [{"key": "notes-tidy", "at": DAY - 3600}]}
        order = []
        for index in range(20):
            chosen = rotation.pick("aster", library, now=DAY + index * 2400)
            if chosen is not None:
                order.append(chosen.key)
        self.assertIn("notes-tidy", order, f"有了一手材料之後還是會輪到：{order}")
        first_tidy = order.index("notes-tidy")
        self.assertTrue(any(kinds[key] in spin.FIRST_ORDER for key in order[:first_tidy]),
                        f"notes-tidy 之前要先有一件帶一手材料的事：{order}")
        closed = [key for key in order if kinds[key] in spin.CLOSED]
        self.assertLessEqual(len(closed) * 3, len(order), f"閉環最多 1/3：{order}")

    def test_cooling_preset_is_skipped(self) -> None:
        rotation = Rotation(self.root / "rot.json")
        library = tuple(item for item in presets_for("nori") if not getattr(item, "once_per_day", False))
        for key in ("game-shop", "game-design", "game-idea", "work-proposal"):
            rotation._novelty.setdefault("nori", {})[key] = {"cooldown_until": DAY + 86400}
        chosen = [rotation.pick("nori", library, now=DAY + index * 4000) for index in range(6)]
        self.assertFalse({p.key for p in chosen if p} & {"game-shop", "game-design", "game-idea",
                                                        "work-proposal"}, "冷卻中的不排")

    def test_note_output_persists(self) -> None:
        path = self.root / "rot.json"
        Rotation(path).note_output("aster", "method", "同じ", now=DAY)
        Rotation(path).note_output("aster", "method", "同じ", now=DAY)
        self.assertEqual(Rotation(path)._novelty["aster"]["method"]["stale"], 1)


class TestNoScriptedFeelings(unittest.TestCase):
    def test_presets_have_no_scripted_private_feelings(self) -> None:
        banned = ("苛立つ", "悔しい", "後ろめたい", "見なかったことにする", "まったく同じ", "青い気がする",
                  "理想論すぎる", "いい加減に書いていた", "破いて考え直す", "何度も書き直す")
        for who, library in PRESETS.items():
            for item in library:
                for text, _visibility in item.steps:
                    for word in banned:
                        self.assertNotIn(word, text, f"{who}/{item.key}: 寫死的情緒「{text}」")


class TestSelfAddedTasks(unittest.TestCase):
    def test_because_required_and_daily_cap(self) -> None:
        import importlib
        import os
        import sys

        root = tempfile.mkdtemp(prefix="ba-tasks-")
        os.environ["TASKS_ROOT"] = root
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "agent" / "tools"))
        try:
            server = importlib.reload(importlib.import_module("mcp_tasks_server"))
            server.TASKS_ROOT = pathlib.Path(root)
            missing = server._call("add", {"title": "読み返す", "entity": "aster"})
            self.assertTrue(missing.get("isError"), "沒寫きっかけ ⇒ 拒")
            for index in range(2):
                ok = server._call("add", {"title": f"試す{index}", "because": "オーナーが言っていた",
                                          "entity": "aster"})
                self.assertFalse(ok.get("isError"), ok)
            third = server._call("add", {"title": "試す3", "because": "外で読んだ", "entity": "aster"})
            self.assertTrue(third.get("isError"), "一天最多 2 件")
        finally:
            os.environ.pop("TASKS_ROOT", None)


class TestRatherRest(unittest.TestCase):
    def test_rest_reason(self) -> None:
        from agent.brain.runtime.inner.willing import rest_reason, roll

        now = 1_800_000_000.0
        calm = {"label": "ふつう", "valence": 0.0, "arousal": 0.0}
        self.assertEqual(rest_reason(calm, entity="aster", now=now), "", "平靜不是累")
        hits = sum(1 for slot in range(200)
                   if rest_reason({"label": "しょんぼり"}, entity="aster", now=now + slot * 600))
        self.assertTrue(60 <= hits <= 140, f"しょんぼり 約一半：{hits}/200")
        self.assertEqual(roll("aster", now), roll("aster", now + 30), "同一格穩定")
        flat = {"label": "ふつう", "valence": -0.2, "arousal": 0.0}
        self.assertTrue(any(rest_reason(flat, entity="nori", now=now + s * 600) == "low_energy"
                            for s in range(30)))

    def test_empty_loop_retries_after_idle(self) -> None:
        from agent.brain.domain.activity import Activity
        from agent.brain.runtime.activity_store import ActivityStore
        from agent.brain.runtime.life_loop import LifeLoop

        clock = {"now": 1000.0}
        asked = []

        def next_activity(entity, previous=None):
            asked.append(clock["now"])
            return None if len(asked) < 2 else Activity.create(entity, "やっと", ["一"])

        loop = LifeLoop("aster", ActivityStore(tempfile.mkdtemp()), lambda a, s: "ok", step_pause=0,
                        next_activity=next_activity, idle_pause=60.0, clock=lambda: clock["now"])
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not asked:
                clock["now"] += 30
                time.sleep(0.05)
            self.assertTrue(asked, "手上空著也會休息到點再問")
            while time.monotonic() < deadline and len(asked) < 2:
                clock["now"] += 30
                time.sleep(0.05)
            self.assertGreaterEqual(len(asked), 2, "再問一次")
        finally:
            loop.shutdown()


class TestWiring(unittest.TestCase):
    class Log:
        def __init__(self):
            self.rows = []

        def append(self, entity, **kwargs):
            self.rows.append({"entity": entity, **kwargs})

    def test_finish_recorder_notes_repeat_honestly(self) -> None:
        from agent.brain.compose.life_wiring import make_finish_recorder

        rotation = Rotation(pathlib.Path(tempfile.mkdtemp()) / "rot.json")
        log = self.Log()
        record = make_finish_recorder(rotation, log)
        activity = NS(entity="aster", id="a1", title="自分の観測記録を整理する", meta={"preset": "notes-tidy"})
        record(activity, close="重複は三件", is_open=False, summary="重複している件を三つ選んだ")
        self.assertEqual(log.rows, [])
        record(activity, close="重複は三件", is_open=False, summary="重複している件を三つ選んだ。")
        self.assertEqual(len(log.rows), 1)
        self.assertEqual(log.rows[0]["visibility"], "private")
        self.assertIn("前と同じようなこと", log.rows[0]["text"])

    def test_rest_check_logs_reason(self) -> None:
        import os
        from unittest import mock

        from agent.brain.compose.life_wiring import make_rest_check

        log = self.Log()
        affect = NS(mood=lambda who: {"label": "しょんぼり"}, timeline=lambda who, limit=12: [])
        rest = make_rest_check(affect, log)
        with mock.patch("agent.brain.runtime.inner.willing.roll", lambda entity, now: 0.0):
            self.assertTrue(rest("mira"))
            with mock.patch.dict(os.environ, {"BLUEAGENT_LIFE_MOOD_REST": "0"}):
                self.assertFalse(rest("mira"))
        self.assertEqual(len(log.rows), 1)
        self.assertEqual((log.rows[0]["kind"], log.rows[0]["visibility"]), ("rest", "private"))


class Clock:
    def __init__(self) -> None:
        self.sleeping = True

    def asleep(self, now):
        return self.sleeping


class TestDmSleep(unittest.TestCase):
    def adapter(self):
        from agent.adapter.app_adapter_store import AdapterSidecar

        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-dm-"))
        clock = Clock()
        handled = []
        adapter = NS(entity="mira", sidecar=AdapterSidecar(root / "mira-cursor.json"),
                     group_attention=NS(circadian={"mira": clock}), names={},
                     _clock=lambda: 1_800_000_000.0, log=lambda m: None)
        adapter._handle = lambda event: handled.append(event)
        return adapter, clock, handled

    def event(self, text, at):
        return {"event_id": f"e{at}", "payload": {"message": {"id": f"m{at}", "created_at": at}}}

    def test_hold_then_read_once_on_waking(self) -> None:
        from agent.adapter import app_dm_sleep

        adapter, clock, handled = self.adapter()
        author = {"id": "owner", "kind": "owner"}
        self.assertTrue(app_dm_sleep.hold(adapter, self.event("ねえ", 1.0), "c1",
                                          {"created_at": 1_799_990_000.0}, author, "ねえ"))
        self.assertTrue(app_dm_sleep.hold(adapter, self.event("寝た？", 2.0), "c1",
                                          {"created_at": 1_799_995_000.0}, author, "寝た？"))
        self.assertEqual(app_dm_sleep.tick(adapter, 1_800_000_000.0), 0, "還在睡 ⇒ 不讀")
        clock.sleeping = False
        self.assertEqual(app_dm_sleep.tick(adapter, 1_800_000_000.0), 1, "醒了 ⇒ 一個會話一輪")
        self.assertEqual(len(handled), 1)
        note = handled[0]["_slept"]
        self.assertIn("いま起きて読んだ", note)
        self.assertIn("オーナー「ねえ」", note)
        self.assertEqual(app_dm_sleep.extra(handled[0]), {"room_note": note})
        self.assertEqual(app_dm_sleep.tick(adapter, 1_800_000_100.0), 0, "讀過就清")

    def test_three_in_a_row_wakes_her(self) -> None:
        from agent.adapter import app_dm_sleep

        adapter, _clock, _handled = self.adapter()
        author = {"id": "owner", "kind": "owner"}
        now = 1_800_000_000.0
        for index in range(2):
            self.assertTrue(app_dm_sleep.hold(adapter, self.event("おーい", index), "c1",
                                              {"created_at": now - 60 + index}, author, "おーい"))
        third = self.event("起きて", 3)
        self.assertFalse(app_dm_sleep.hold(adapter, third, "c1", {"created_at": now}, author, "起きて"))
        self.assertIn("何度も呼ばれて目が覚めた", third["_slept"])

    def test_awake_passes_through(self) -> None:
        from agent.adapter import app_dm_sleep

        adapter, clock, _handled = self.adapter()
        clock.sleeping = False
        self.assertFalse(app_dm_sleep.hold(adapter, self.event("x", 1), "c1", {}, {"id": "owner"}, "x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
