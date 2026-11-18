#!/usr/bin/env python3
"""作品 vs 經歷（owner 2026-11-16：「算她做過的事，但不算她經歷過的事」）。

實測起點：mira 22:52 的私聊「さっきまで港の灯りを数えてたの…」——那內容來自她
`self_note` 裡寫的場景（`work-scenario` 活動），卻被記成她親身經歷，於是搭話時說「我在港口」。
這支釘住四件事：

1. `facts.fiction` 落庫／讀回；
2. 檢索預設不收作品（`BLUEAGENT_RECALL_FICTION=1` 才放回，給「談創作」用）；
3. 自傳式記憶不收作品（「我確實是這樣的人」不能是虛構場景）；
4. 日記只把作品列成「今天寫的東西」，並另寫一條 `fiction=1` 的事實。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "agent" / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent.adapter.memory_sqlite import SqliteMemory  # noqa: E402
from agent.brain.runtime import reflection as reflection_mod  # noqa: E402
from agent.brain.runtime.inner import presets, self_model  # noqa: E402


class FictionFactCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="ba-fiction-")
        self.addCleanup(self.dir.cleanup)
        self.path = pathlib.Path(self.dir.name) / "facts.db"
        self.memory = SqliteMemory(self.path, seed=False)

    def test_fiction_roundtrips(self):
        self.memory.add("港の灯りを数えた場面を書いた", layer="self", entity="mira",
                        source="self_note", fiction=1, kind="event")
        self.memory.add("今日は雨だった", layer="self", entity="mira",
                        source="activity_public", kind="event")
        facts = self.memory.all_facts("mira") if hasattr(self.memory, "all_facts") else []
        if not facts:                                  # 走一般查詢
            facts = [self.memory._row_to_fact(row) for row in self.memory._db.execute(
                "SELECT * FROM facts WHERE entity = 'mira'")]
        marks = {fact.text: fact.fiction for fact in facts}
        self.assertEqual(marks["港の灯りを数えた場面を書いた"], 1)
        self.assertEqual(marks["今日は雨だった"], 0)

    def test_recall_excludes_works_by_default(self):
        from agent.brain.runtime.memory import attention_field
        self.memory.add("港の灯りを数えた場面を書いた", layer="self", entity="mira",
                        source="self_note", fiction=1)
        self.memory.add("港で潮風に当たった", layer="self", entity="mira",
                        source="activity_public", fiction=0)
        pool = [self.memory._row_to_fact(row) for row in self.memory._db.execute(
            "SELECT * FROM facts WHERE entity = 'mira'")]
        kept = attention_field.drop_fiction(pool)
        self.assertEqual([fact.text for fact in kept], ["港で潮風に当たった"])
        os.environ["BLUEAGENT_RECALL_FICTION"] = "1"
        try:
            self.assertEqual(len(attention_field.drop_fiction(pool)), 2)
        finally:
            os.environ.pop("BLUEAGENT_RECALL_FICTION", None)


class SelfModelScopeCase(unittest.TestCase):
    def test_fiction_is_not_a_self_portrait_source(self):
        rows = [{"id": 1, "text": "主人のことは「オーナー」と呼ぶ。", "kind": "rule",
                 "layer": "pair", "audience": "", "principal": "owner", "game": 0,
                 "fiction": 0, "created_at": 1000.0},
                {"id": 2, "text": "港の灯りを数える私", "kind": "event", "layer": "self",
                 "audience": "", "principal": "", "game": 0, "fiction": 1,
                 "created_at": 1000.0}]
        stable, recent = self_model.build(rows, now=5000.0)
        joined = " ".join(stable + recent)
        self.assertIn("オーナー", joined)
        self.assertNotIn("港の灯り", joined)


class ReflectionSplitCase(unittest.TestCase):
    def test_works_are_listed_but_not_told_as_experience(self):
        class Log:
            def entries(self, entity, limit=12, visibility=None):
                return [{"text": "港の灯りを数えた場面を書いた", "kind": "work",
                         "activity": "卓で使える短い場面を一つ書く", "visibility": "public",
                         "fiction": 1},
                        {"text": "台所を片づけた", "kind": "work", "activity": "片付け",
                         "visibility": "public", "fiction": 0}]

        memory = _Recorder()
        daily = reflection_mod.DailyReflection(root=pathlib.Path(tempfile.mkdtemp()),
                                               memory=memory, activity_log=Log(),
                                               call=lambda prompt: "一行目\n二行目",
                                               entities=("mira",), clock=lambda: 1000.0)
        daily.reflect_once("mira")
        material, _room, works = daily._material("mira")
        self.assertIn("台所を片づけた", material)
        self.assertNotIn("港の灯りを数えた場面を書いた", material)
        self.assertEqual(works, ["卓で使える短い場面を一つ書く"])
        kinds = [(item["source"], item.get("fiction", 0)) for item in memory.added]
        self.assertIn(("reflection", 0), kinds)
        self.assertIn(("reflection_work", 1), kinds)


class _Recorder:
    def __init__(self):
        self.added = []

    def add(self, text, **kwargs):
        self.added.append({"text": text, **kwargs})


class PresetMarkingCase(unittest.TestCase):
    def test_scenario_preset_is_marked_fiction(self):
        library = presets.library("mira") if hasattr(presets, "library") else []
        keys = {preset.key: preset for preset in library}
        if "work-scenario" in keys:
            self.assertTrue(keys["work-scenario"].fiction)
        else:                                          # 走常數表
            found = [preset for preset in getattr(presets, "PRESETS", {}).get("mira", ())
                     if preset.key == "work-scenario"]
            self.assertTrue(found and found[0].fiction)

    def test_other_works_are_not_fiction(self):
        rows = [preset for preset in getattr(presets, "PRESETS", {}).get("mira", ())
                if preset.key in {"work-sheet", "review-quiz"}]
        for preset in rows:
            self.assertFalse(preset.fiction, f"{preset.key} 不是虛構作品")


if __name__ == "__main__":
    unittest.main()
