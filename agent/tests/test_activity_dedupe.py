#!/usr/bin/env python3
"""活動日誌的去重口徑（owner 2026-10-24 第②項）。

舊行為：去重比「活動標題 + 步驟」⇒ 同一個活動做第二輪時每一格都被當成重複、
**不寫**。實測後果：她一天 1050 步，公開日誌只有 3 條（第一輪），跟她實際做的事脫節。

新口徑：去重只認 **activity_id**——同一件活動的同一格重跑（崩潰／重啟續跑）不重複記帳，
下一輪同名活動照記。
"""

from __future__ import annotations

import collections
import json
import pathlib
import tempfile
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.activity import Activity
from agent.brain.domain.entity import EntityRegistry
from agent.brain.runtime.life import make_step_fn
from agent.brain.memory.in_memory import InMemoryMemory
from agent.brain.runtime.activity_log import PUBLIC, ActivityLog


class TestDedupeScope(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-act-"))
        self.log = ActivityLog(self.root)

    def _rows(self):
        path = self.root / "activity-mira.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_same_activity_step_is_written_once(self):
        activity = Activity.create("mira", "學判定", ["一", "一"])
        first = self.log.append("mira", kind="study", text="做了第一步",
                                visibility=PUBLIC, activity=activity.title,
                                step=0, activity_id=activity.id)
        again = self.log.append("mira", kind="study", text="做了第一步",
                                visibility=PUBLIC, activity=activity.title,
                                step=0, activity_id=activity.id)
        self.assertFalse(first.get("duplicate"))
        self.assertTrue(again.get("duplicate"), "同一件的同一格重跑 ⇒ 只記一次")
        self.assertEqual(len(self._rows()), 1)

    def test_next_round_with_same_title_is_recorded(self):
        """同一套活動再排一次是新的一件 ⇒ 必須照記（舊版就是這裡被吞掉）。"""
        for round_index in range(3):
            activity = Activity.create("mira", "學判定", ["一", "二"])
            self.log.append("mira", kind="study", text=f"第 {round_index} 輪第一步",
                            visibility=PUBLIC, activity=activity.title,
                            step=0, activity_id=activity.id)
        rows = self._rows()
        self.assertEqual(len(rows), 3, rows)
        self.assertEqual(len({row["activity_id"] for row in rows}), 3)

    def test_step_and_visibility_still_separate(self):
        activity = Activity.create("mira", "學判定", ["一"])
        self.log.append("mira", kind="study", text="public 版", visibility=PUBLIC,
                        activity=activity.title, step=0, activity_id=activity.id)
        self.log.append("mira", kind="study", text="private 版", visibility="private",
                        activity=activity.title, step=0, activity_id=activity.id)
        self.assertEqual(len(self._rows()), 2)

    def test_callers_without_activity_id_are_never_deduped(self):
        """心跳／語音／TRPG 那些沒有 activity_id 的來源：照舊全寫。"""
        for index in range(3):
            self.log.append("mira", kind="heartbeat", text="想說話",
                            visibility=PUBLIC, activity="")
        self.assertEqual(len(self._rows()), 3)


class TestNoteAndFactNotDuplicated(unittest.TestCase):
    """`step_fn` 重跑同一格時，筆記與事實都不再寫第二份。"""

    # 每步寫記憶 2026-11-03 起預設關掉（降採樣）；這裡打開它，驗「開著時重跑也不寫第二份」
    @mock.patch.dict("os.environ", {"BLUEAGENT_LIFE_STEP_FACTS": "1"})
    def test_duplicate_step_skips_note_and_fact(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-step-"))
        log = ActivityLog(root / "logs")
        memory = InMemoryMemory()
        write = make_step_fn(memory, EntityRegistry(), log=log, notes_root=root / "notes",
                             call=lambda prompt: "規則書のプッシュロールを読んだ")

        activity = Activity.create("mira", "學判定", ["一"])
        activity.steps[0].visibility = PUBLIC
        write(activity, activity.steps[0])
        write(activity, activity.steps[0])            # 模擬崩潰後重跑同一格

        note = (root / "notes" / "mira").glob("*.md")
        text = next(iter(note)).read_text(encoding="utf-8")
        self.assertEqual(text.count("學判定"), 1, text)
        labels = collections.Counter(row.get("label") for row in memory.snapshot()["facts"])
        self.assertEqual(labels.get("她的筆記", 0), 1, labels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
