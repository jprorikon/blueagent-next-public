#!/usr/bin/env python3
"""房間：世界變化與分層記憶**共用的那一個鍵**（owner 2026-11-03「語義對齊」）。

判據：
  ① 房間的四種 label 判得對（world／self／pair／circle），且 `self_room` 默認是「她＋主人」——
     只寫「她」會讓主人在場時她連自己的日常都想不起來（在場 ⊆ 房間）；
  ② 世界變化的「人」行**按房間聚合**，label 與記憶層同一套（`pair(owner)`／`circle(n)`）；
  ③ 公開產出那條路（筆記會進公開日誌）**不給別人的原話**；獨白那條路可以。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest
import os

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))   # 倉庫根

from agent.brain.runtime.inner.rooms import (  # noqa: E402
    audience_of, human_label, label_of, room_of_thread, self_room)
from agent.brain.domain.activity import Activity  # noqa: E402
from agent.brain.runtime.life_step import make_step_fn  # noqa: E402
from agent.brain.runtime.inner.world_delta import (  # noqa: E402
    WorldDelta, talked_from_threads)
from prompt_lang import chinese_hits  # noqa: E402

T0 = 1793683200.0
HOUR = 3600.0


class TestLabel(unittest.TestCase):
    def test_four_kinds(self):
        self.assertEqual(label_of(None), "world")
        self.assertEqual(label_of(set()), "world")
        self.assertEqual(label_of({"mira"}, entity="mira"), "self")
        self.assertEqual(label_of({"mira", "owner"}, entity="mira"), "pair(owner)")
        self.assertEqual(label_of({"mira", "owner", "alice"}, entity="mira"), "circle(2)")

    def test_audience_of(self):
        self.assertEqual(audience_of("mira", ["owner"]), {"mira", "owner"})
        self.assertEqual(audience_of("", []), set())

    def test_self_room_is_hers_alone(self):
        """「她和主人」和「僅她自己」是兩個域（owner 2026-11-03）：self 只有她自己。"""
        self.assertEqual(self_room("mira"), {"mira"})
        self.assertEqual(self_room("mira", scope="pair_owner"), {"mira", "owner"})

    def test_room_of_thread_includes_her(self):
        thread = types.SimpleNamespace(participants={"owner"})
        self.assertEqual(room_of_thread(thread, entity="mira"), {"mira", "owner"})

    def test_human_labels_are_japanese(self):
        for label in ("world", "self", "pair(owner)", "pair(alice)", "circle(3)"):
            with self.subTest(label=label):
                text = human_label(label)
                self.assertEqual(chinese_hits(text), [], text)
        self.assertEqual(human_label("pair(owner)"), "個別（主人）")


class _Turn:
    def __init__(self, role: str, text: str, at: float) -> None:
        self.role = role
        self.text = text
        self.at = at
        self.speaker = "owner" if role != "assistant" else "mira"


class _Thread:
    def __init__(self, *, entity: str, participants, turns) -> None:
        self.key = types.SimpleNamespace(entity=entity)
        self.participants = set(participants)
        self.transcript = list(turns)


class _Threads:
    def __init__(self, rows) -> None:
        self.rows = list(rows)

    def threads(self):
        return tuple(self.rows)


class TestTalkedByRoom(unittest.TestCase):
    def _delta(self, root, threads):
        return WorldDelta(root / "world", "mira", tz="Asia/Tokyo",
                          talked=talked_from_threads(threads), notes_root=None)

    def _threads(self):
        return _Threads([
            _Thread(entity="mira", participants={"mira", "owner"},
                    turns=[_Turn("user", "今日ひま？", T0 + 60),
                           _Turn("assistant", "うん", T0 + 61),
                           _Turn("user", "晩ごはん食べた", T0 + 120)]),
            _Thread(entity="mira", participants={"mira", "owner", "alice"},
                    turns=[_Turn("user", "みんなで遊ぼう", T0 + 200)]),
            _Thread(entity="aster", participants={"aster", "owner"},      # 別人的線不該進來
                    turns=[_Turn("user", "アスターいる？", T0 + 60)]),
        ])

    def test_events_are_grouped_by_room(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-rooms-"))
        delta = self._delta(root, self._threads())
        delta.block(now=T0)                       # 先建立錨點
        text = delta.block(now=T0 + HOUR)
        self.assertIn("3 回", text)                # 2（一對一）＋ 1（群）
        self.assertIn("個別（主人） 2 回", text)
        self.assertIn("2人の場 1 回", text)
        self.assertNotIn("アスター", text)             # 別人的線不進來

    def test_public_path_hides_what_people_said(self):
        # 兩條路各自一個 data 目錄（錨點是共用的，不能拿同一份連續呼叫兩次）
        root_private = pathlib.Path(tempfile.mkdtemp(prefix="ba-rooms-"))
        delta = self._delta(root_private, self._threads())
        delta.block(now=T0)
        private = delta.block(now=T0 + HOUR)
        self.assertIn("晩ごはん", private)          # 獨白那條路：看得到
        root_public = pathlib.Path(tempfile.mkdtemp(prefix="ba-rooms-"))
        delta_pub = self._delta(root_public, self._threads())
        delta_pub.block(now=T0)
        public = delta_pub.block(now=T0 + HOUR, public=True)
        self.assertIn("個別（主人）", public)       # 公開那條路：只給房間與次數
        self.assertNotIn("晩ごはん", public)
        self.assertNotIn("みんなで", public)

    def test_no_threads_means_unknown(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-rooms-"))
        delta = self._delta(root, _Threads([]))
        delta.block(now=T0)
        text = delta.block(now=T0 + HOUR)
        self.assertNotIn("誰も来なかった", text)


class _Memory:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def add(self, text, **kwargs):
        self.calls.append({"text": text, **kwargs})


class _Entities:
    def get(self, entity_id):
        return types.SimpleNamespace(id=entity_id, name="ミラ")


class TestSelfFactsCarryARoom(unittest.TestCase):
    """她自己那層事實要帶房間——沒帶就是「世界可見」（PENDING A-4 的那條欠賬）。"""

    def tearDown(self):
        os.environ.pop("BLUEAGENT_LIFE_STEP_FACTS", None)

    def _run(self):
        memory = _Memory()

        def work(title, step, **kwargs):
            return {"ok": True, "text": "三本の記事を読んで、判定の作り方が分かった。",
                    "trace": [{"name": "mcp_firecrawl_firecrawl_search", "ok": True}]}

        activity = Activity.create("mira", "記事を調べる", ["調べる"])
        step_fn = make_step_fn(memory, _Entities(), duration=0.0, work=work, notes_root=None)
        step_fn(activity, activity.current)
        return activity, memory

    def test_summary_fact_has_the_self_room(self):
        activity, memory = self._run()
        rows = [call for call in memory.calls if call.get("layer") == "self"]
        self.assertTrue(rows, "收尾摘要應該沉一條 self 事實")
        for call in rows:
            self.assertEqual(call.get("audience"), {"mira"},
                             f"沒帶房間＝世界可見：{call['text'][:30]}")

    def test_step_facts_when_enabled_also_carry_it(self):
        os.environ["BLUEAGENT_LIFE_STEP_FACTS"] = "1"
        _activity, memory = self._run()
        rows = [call for call in memory.calls if call.get("source") == "self_note"]
        self.assertTrue(rows)
        self.assertEqual(rows[0].get("audience"), {"mira"})



class TestSelfLayerReadable(unittest.TestCase):
    """線路 A 的範圍（2026-11-03 review）：只有**房間裡只有她**的 self 事實繞過披露閘。

    `reflection.py` 的日記素材是主人在場的對話原文、房間是那幾段的交集 ⇒ 帶別人的 self 事實
    要照舊過閘，不然主人私聊寫成的日記會在客人的聊天裡被召回（實測）。
    """

    def _memory(self):
        from agent.adapter.memory_sqlite import SqliteMemory
        path = pathlib.Path(tempfile.mkdtemp(prefix="ba-self-")) / "facts.db"
        return SqliteMemory(path, seed=False, entity_ids=("mira",))

    def _recall(self, memory, principal, present, query):
        from agent.brain.domain.thread import MemoryScope
        scope = MemoryScope(principal=principal, entity="mira", scene="chat",
                            thread_id="t", self_owned=False)
        return [fact.text for fact in memory.recall_detail(scope, query, participants=set(present))]

    def test_diary_with_owner_is_not_recalled_in_a_guest_chat(self):
        memory = self._memory()
        memory.add("今日はオーナーがアリスの誕生日サプライズの準備をこっそり相談してくれた", layer="self",
                   label="她的日記", self_owned=True, audience={"mira", "owner"},
                   entity="mira", source="reflection", kind="event")
        self.assertEqual(self._recall(memory, "alice", {"alice", "mira"}, "誕生日サプライズ"), [],
                         "主人私聊寫成的日記不能在客人面前被召回")
        self.assertTrue(self._recall(memory, "owner", {"owner", "mira"}, "誕生日サプライズ"),
                        "主人自己來問，照樣想得起來")

    def test_her_own_notes_are_readable_anywhere(self):
        memory = self._memory()
        memory.add("判定表を一枚にまとめ直した", layer="self", label="她的成果", self_owned=True,
                   audience={"mira"}, entity="mira", source="activity_summary", kind="event")
        self.assertTrue(self._recall(memory, "alice", {"alice", "mira"}, "判定表"),
                        "她自己的成果（房間只有她）照線路 A 永遠想得起來")


if __name__ == "__main__":
    unittest.main(verbosity=2)
