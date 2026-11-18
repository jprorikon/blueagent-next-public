#!/usr/bin/env python3
"""她的活動會**輪換**（owner 2026-10-24：「1,2,3 都做」的第①項）。

之前的實測：活動庫只有一套、`next_activity` 永遠回同一套 ⇒ 同一個三步循環一天跑
1050 步（筆記 3000+ 行全是複讀）。判據：

  ① 相鄰兩次不排同一套；
  ② 一輪（= 活動庫大小）之內每一套都出現過，不會偏食；
  ③ 今天做過最多次的排到最後；
  ④ 時段限制：`hours` 窗口外的活動在該時段不被排到（除非全部都在窗外）；
  ⑤ `LifeLoop` 收尾時會把**剛做完的那一件**交給 `next_activity`（兩參數版本）。
"""

from __future__ import annotations

import pathlib
import tempfile
import time

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.life import (Rotation, build_activity, preset_key_of,
                                      presets_for)
from agent.brain.runtime.life_loop import LifeLoop, _takes_previous
from agent.brain.runtime.activity_store import ActivityStore
from agent.brain.domain.activity import Activity

ENTITIES = ("mira", "aster", "nori")


class TestRotationPicks(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-rot-"))
        self.rotation = Rotation(self.root / "life-rotation.json")
        self.now = time.mktime((2026, 10, 24, 15, 0, 0, 0, 0, -1))

    def test_no_immediate_repeat_and_full_cycle(self):
        for entity in ENTITIES:
            # 每日報（once_per_day）只在自己時段內出現，不參與「一輪全覆蓋」的斷言
            library = tuple(item for item in presets_for(entity)
                            if not getattr(item, "once_per_day", False))
            seen = []
            for index in range(len(library) * 2):
                preset = self.rotation.pick(entity, library,
                                            now=self.now + index * 300)
                seen.append(preset.key)
            for before, after in zip(seen, seen[1:]):
                self.assertNotEqual(before, after, f"{entity}: 連做同一套 {seen}")
            # 防自旋（2026-11-09）：閉環的（讀自己／同伴產出的）要有新材料、一天最多 1/3，
            # 所以「一輪之內每套都做到」只對非閉環的成立
            from agent.brain.runtime.inner.spin import is_closed
            from agent.brain.runtime.life import _in_window
            hour = time.localtime(self.now).tm_hour
            open_keys = {item.key for item in library
                         if not is_closed(item) and _in_window(hour, item.hours)}
            self.assertLessEqual(open_keys, set(seen[:len(library)]),
                                 f"{entity}: 一輪之內非閉環的每套都要做到 {seen}")
            closed = sum(1 for key in seen if key not in open_keys)
            self.assertLessEqual(closed * 3, len(seen), f"{entity}: 閉環最多 1/3 {seen}")

    def test_state_survives_restart(self):
        library = presets_for("aster")
        first = [self.rotation.pick("aster", library, now=self.now + i).key for i in range(3)]
        restored = Rotation(self.root / "life-rotation.json")
        self.assertEqual([row["key"] for row in restored.history("aster")], first,
                         "輪換要落盤：重啟不能又從第一套開始")
        again = restored.pick("aster", library, now=self.now + 999)
        self.assertNotEqual(again.key, first[-1])

    def test_least_done_today_goes_first(self):
        library = presets_for("nori")
        key = library[0].key
        for offset in range(3):
            self.rotation.pick("nori", [library[0]], now=self.now + offset)
        counts = {}
        for offset in range(len(library)):
            chosen = self.rotation.pick("nori", library, now=self.now + 600 + offset)
            counts[chosen.key] = counts.get(chosen.key, 0) + 1
        self.assertNotIn(key, [k for k, v in counts.items() if v >= 2],
                         f"做過最多次的不該被偏食 {counts}")

    def test_hours_window_is_respected(self):
        library = presets_for("mira")
        night_titles = []
        for offset in range(len(library) * 2):
            # 03:00 ⇒ 只有 hours=(9,23) 的那一套在窗外
            chosen = self.rotation.pick("mira", library,
                                        now=time.mktime((2026, 10, 24, 3, 0, 0, 0, 0, -1)) + offset * 300)
            night_titles.append(chosen.key)
        self.assertNotIn("actual-play", night_titles, night_titles)

    def test_corrupt_file_is_not_fatal(self):
        path = self.root / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        rotation = Rotation(path)
        chosen = rotation.pick("mira", presets_for("mira"), now=self.now)
        self.assertTrue(chosen.key)
        self.assertTrue(path.exists())


class TestDemoActivityCarriesPresetKey(unittest.TestCase):
    def test_key_round_trips(self):
        activity = build_activity("aster", hour=12)
        self.assertEqual(preset_key_of(activity), activity.meta["preset"])
        self.assertEqual(preset_key_of(None), "")

    def test_after_excludes_previous(self):
        library = presets_for("nori")
        first = build_activity("nori", hour=12)
        second = build_activity("nori", hour=12, after=preset_key_of(first))
        self.assertNotEqual(preset_key_of(first), preset_key_of(second),
                            "手動排下一件時也不要排到同一套")
        self.assertIn(preset_key_of(second), [item.key for item in library])


class TestLifeLoopHandsOverPrevious(unittest.TestCase):
    def test_two_arg_callback_receives_finished_activity(self):
        store = ActivityStore(tempfile.mkdtemp(prefix="ba-hand-"))
        seen: list[str] = []
        done = {"now": 0}

        def next_activity(entity, previous):
            seen.append(preset_key_of(previous))
            done["now"] += 1
            if done["now"] >= 2:
                return None
            return Activity.create(entity, "第二件", ["一"])

        first = Activity.create("mira", "第一件", ["一"])
        first.meta["preset"] = "alpha"
        loop = LifeLoop("mira", store, lambda activity, step: "ok",
                        step_pause=0.005, next_activity=next_activity,
                        between_pause=0.0)
        try:
            loop.start(first)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(seen) < 2:
                time.sleep(0.02)
            self.assertEqual(seen[0], "alpha", f"要拿到剛做完那一件的 preset key：{seen[:3]}")
            self.assertIn("", seen[1:3], f"第二件沒有 preset 標記 ⇒ 空字串：{seen[:3]}")
        finally:
            loop.shutdown()

    def test_signature_detection(self):
        self.assertTrue(_takes_previous(lambda entity, previous: None))
        self.assertFalse(_takes_previous(lambda entity: None))
        self.assertFalse(_takes_previous(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
