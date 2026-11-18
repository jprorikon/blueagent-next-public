#!/usr/bin/env python3
"""生活流降採樣 ＋ 主題聚合（owner 2026-11-03 第 3 步）。

判據：
  ① **每一步不再寫進記憶**（預設關；`BLUEAGENT_LIFE_STEP_FACTS=1` 可開回來）；
     —— 實測生活流水是對話事實的 ~30 倍，600 條窗口 2.5 天換一輪，對話記憶整片消失；
  ② 每件事收尾那一條摘要**要帶主題**（preset key，穩定；改標題也不動）；
  ③ `life_topics()` 把 4 萬條碎片聚成幾條**主題線**（件數／最近一次／最近那一句）。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.adapter.memory_sqlite import SqliteMemory  # noqa: E402
from agent.brain.domain.activity import Activity  # noqa: E402
from agent.brain.runtime.life_step import _step_facts_on, _topic_of  # noqa: E402
from agent.brain.runtime.memory.memory_search import MemorySearch  # noqa: E402


class TestStepFactGate(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("BLUEAGENT_LIFE_STEP_FACTS", None)

    def test_default_is_off(self):
        os.environ.pop("BLUEAGENT_LIFE_STEP_FACTS", None)
        self.assertFalse(_step_facts_on())

    def test_can_be_turned_back_on(self):
        for value in ("1", "on", "true"):
            with self.subTest(value=value):
                os.environ["BLUEAGENT_LIFE_STEP_FACTS"] = value
                self.assertTrue(_step_facts_on())


class TestTopicOf(unittest.TestCase):
    def test_preset_key_wins(self):
        activity = Activity.create("mira", "ノートを読み返す", ["a", "b"])
        activity.meta["preset"] = "notes-reread"
        self.assertEqual(_topic_of(activity), "notes-reread")

    def test_title_is_the_fallback(self):
        activity = Activity.create("mira", "題のない活動", ["a"])
        self.assertEqual(_topic_of(activity), "題のない活動")


class TestLifeTopics(unittest.TestCase):
    def test_fragments_collapse_into_topic_lines(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-topics-"))
        db = root / "facts.db"
        memory = SqliteMemory(db, entity_ids=("mira",))
        for step in range(4):
            memory.add(f"ノートを読み返した（{step}）", source="activity_summary",
                       entity="mira", layer="self", topic="notes-reread")
        memory.add("ストアで新作を探した", source="activity_summary", entity="mira",
                   layer="self", topic="game-shop")
        memory.add("主人とキャラクターシートの話をした", source="distill", entity="mira",
                   layer="pair", audience={"mira", "owner"}, principal="owner")
        search = MemorySearch(memory=memory, db_path=db)
        topics = search.life_topics("mira", limit=5)
        memory.close()
        by_topic = {row["topic"]: row for row in topics}
        self.assertEqual(by_topic["notes-reread"]["count"], 4)
        self.assertEqual(by_topic["game-shop"]["count"], 1)
        self.assertIn("ノートを読み返した", by_topic["notes-reread"]["latest"])
        # 對話流不進主題索引（那是 life 流的東西）
        self.assertNotIn("キャラクターシート", "".join(row["topic"] for row in topics))


if __name__ == "__main__":
    unittest.main(verbosity=2)
