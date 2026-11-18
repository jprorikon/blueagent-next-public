#!/usr/bin/env python3
"""時域第二步：同槽位覆蓋（新值取代舊值）＋ as-of 查詢＋「昨天／上次」時間意図。

判據：
  ① 同一個 slot 寫新值 ⇒ 舊值 `valid_to=now`、`superseded_by=新值`（不刪）；
  ② 現在問 ⇒ 只回新值；
  ③ 問「以前／上次」⇒ 連舊值一起回（可回溯）；
  ④ 問「昨天」⇒ as-of 昨天，回當時成立的那一版；
  ⑤ 事件過期後：現在不注入，但問「那天」仍查得到。
"""

from __future__ import annotations

import pathlib
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.runtime.memory.memory_search import MemorySearch


class TestSupersede(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "facts.db"
        self.memory = SqliteMemory(self.path, seed=False, entity_ids=("mira",))
        self.search = MemorySearch(memory=self.memory, db_path=self.path)

    def put(self, text, *, slot="", kind="state", at=None, days=0.0):
        when = float(at if at is not None else time.time())
        self.memory.supersede_slot(entity="mira", slot=slot, new_key=text, at=when)
        return self.memory.add(text, layer="pair", audience={"mira", "owner"},
                               principal="owner", entity="mira", source="test",
                               kind=kind, slot=slot, valid_from=when,
                               valid_to=(when + days * 86400 if days else 0.0))

    def rows(self, query, present=("mira", "owner")):
        out = self.search.search(entity="mira", present=set(present), query=query,
                                 limit=5, layers=("fact",))
        return [row["text"] for row in out["facts"]], out.get("time_intent")

    def test_new_state_supersedes_old(self):
        self.put("主人正在讀的書是《佐々木とピーちゃん》", slot="主人正在讀的書",
                 at=time.time() - 86400 * 10)
        self.put("主人正在讀的書是《另一本》", slot="主人正在讀的書")
        active = [fact for fact in
                  [self.memory._row_to_fact(row) for row in
                   self.memory._db.execute("SELECT * FROM facts").fetchall()]
                  if fact.is_active()]
        self.assertEqual([fact.text for fact in active], ["主人正在讀的書是《另一本》"],
                         "現在只該有一條現值")

    def test_vague_past_question_sees_the_old_value_of_that_topic(self):
        """籠統的「以前」＋話題檢索（owner 2026-11-03）：問的是哪個槽，就帶出那個槽的舊值，並標成以前的事。"""
        past = time.time() - 86400 * 10
        self.put("主人正在讀的書是《佐々木とピーちゃん》", slot="主人正在讀的書", at=past)
        self.put("主人正在讀的書是《另一本》", slot="主人正在讀的書")
        texts, intent = self.rows("以前我在讀什麼書？")
        self.assertTrue(intent["past"])
        old = [text for text in texts if "佐々木" in text]
        self.assertTrue(old, f"同話題的舊值要查得到：{texts}")
        self.assertTrue(all(text.startswith("（以前の話）") for text in old), f"舊值要標成以前：{old}")
        self.assertTrue(any("另一本" in text and not text.startswith("（以前") for text in texts),
                        f"現值也該在、而且不帶標記：{texts}")

    def test_vague_past_does_not_revive_other_topics(self):
        """「船長」那條：問的是書，稱呼槽沒有現值命中 ⇒ 被取代的舊稱呼不會因為「以前」就復活。"""
        past = time.time() - 86400 * 10
        self.put("主人正在讀的書是《另一本》", slot="主人正在讀的書")
        self.put("主人的稱呼是船長，正在讀的書很多", slot="主人的稱呼", at=past)
        self.put("主人的稱呼是オーナー", slot="主人的稱呼")
        texts, _intent = self.rows("以前我在讀什麼書？")
        self.assertFalse(any("船長" in text for text in texts), f"別的話題的舊值不該復活：{texts}")

    def test_as_of_yesterday_returns_the_old_version(self):
        old_at = time.time() - 86400 * 5
        self.put("主人正在讀的書是《佐々木とピーちゃん》", slot="主人正在讀的書", at=old_at)
        self.put("主人正在讀的書是《另一本》", slot="主人正在讀的書")
        texts, intent = self.rows("兩天前我在讀什麼書？")
        self.assertEqual(intent["past"], True)
        self.assertTrue(any("佐々木" in text for text in texts), texts)

    def test_expired_event_hidden_now_but_visible_for_that_day(self):
        when = time.time() - 86400 * 3
        self.put("今晚一起準備角色卡", slot="", kind="event", at=when, days=0.5)
        now_texts, _ = self.rows("角色卡準備到哪了")
        self.assertEqual(now_texts, [], "過期事件現在不該回")
        past_texts, intent = self.rows("那天角色卡準備到哪了")
        self.assertTrue(intent["past"])
        self.assertTrue(any("角色卡" in text for text in past_texts), past_texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
