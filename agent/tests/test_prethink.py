#!/usr/bin/env python3
"""提前想（idle pre-think）：閒時先想，下一輪直接用，且**不吃回話時間**。

判據：
  ① 閒夠久 ＋ 有幾輪原文 ⇒ 產出一段預想並落檔；
  ② 只用一次（`take` 之後就作廢）；過期也作廢；
  ③ 不閒／太頻繁／沒設定 ⇒ 不跑（回 `ok=False` 與原因），不拋例外；
  ④ 下一輪由 `SubAgentStage` 當成 `[prethink]` 筆記帶進 prompt（不是對話、不進記憶）。

2026-11-09 檢查後補（`TestPreThinkCost`／`TestPreThinkFreshness`／`TestPreThinkMaterial`）：
  ⑤ 素材沒變就不再想（以前閒著每 2 分鐘重想同一段，8 小時 239 次）；
  ⑥ 新不新鮮按對話的時間——昨晚的話不能今早當「さっき」帶進來；
  ⑦ 睡著／預算不夠不想；
  ⑧ 角色之間的私聊不當成「她自己的事」拿來想；逐字稿寫わたし／オーナー，不是 owner／assistant。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.thread import ThreadKey, TurnRecord
from agent.brain.runtime.prethink import PreThinker
from agent.brain.runtime.rawlog import RawLog


class FakeThread:
    def __init__(self, entity, thread_id, turns):
        self.key = ThreadKey(scope="owner", entity=entity, scene="chat", purpose="talk")
        self.id = thread_id
        self.participants = {"owner"}
        self.segment = list(turns)


def turn(turn_id, text, *, at, role="user"):
    return TurnRecord(turn_id=turn_id, role=role, text=text, source="app", at=at,
                      speaker="owner" if role == "user" else "mira")


class TestPreThink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.log = RawLog(self.root / "raw")
        now = time.time() - 600
        self.log.sync_thread(FakeThread("mira", "th-1", [
            turn("t1", "今、小説読んでるよ。佐々木とピーちゃん。", at=now),
            turn("t2", "へえ、どんな話？", at=now + 20, role="assistant"),
            turn("t3", "異世界とこっちを行き来する話。六巻まで読んだ。", at=now + 40),
        ]))
        self.calls = {"n": 0}

    def think(self, note="有點想知道他讀到哪裡，也想推薦自己喜歡的那段；怕劇透所以先問。", **kwargs):
        def call(prompt: str) -> str:
            self.calls["n"] += 1
            self.prompt = prompt
            return note

        return PreThinker(raw_log=self.log, call=call, root=self.root / "prethink",
                          entities=["mira"], idle_s=60, interval_s=30,
                          max_age_s=1800, **kwargs)

    def test_think_writes_note_and_is_used_once(self):
        thinker = self.think()
        result = thinker.think_once("mira")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.calls["n"], 1)
        self.assertIn("ここ最近のやりとり", self.prompt)
        self.assertIn("佐々木", self.prompt)
        note = thinker.take("mira", present={"owner"})
        self.assertIn("劇透", note)
        self.assertEqual(thinker.take("mira", present={"owner"}), "", "預想只用一次")

    def test_not_idle_skips(self):
        thinker = self.think()
        thinker.idle_s = 99999          # 距上一輪才 10 分鐘
        result = thinker.think_once("mira")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_idle")
        self.assertEqual(self.calls["n"], 0)

    def test_too_soon_skips(self):
        thinker = self.think()
        thinker.think_once("mira")
        again = thinker.think_once("mira")
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "too_soon")
        self.assertEqual(self.calls["n"], 1)

    def test_expired_note_is_discarded(self):
        import json
        thinker = self.think()
        thinker.think_once("mira")
        path = self.root / "prethink" / "mira.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        row["at"] = time.time() - 5000          # 假裝是 1 小時 20 分鐘前想的
        path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(thinker.take("mira", present={"owner"}), "", "過期的預想不該再用")

    def test_failure_does_not_raise(self):
        def broken(prompt: str) -> str:
            raise RuntimeError("boom")

        thinker = PreThinker(raw_log=self.log, call=broken, root=self.root / "prethink",
                             entities=["mira"], idle_s=60, interval_s=30)
        result = thinker.think_once("mira")
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["reason"])


class TestPreThinkDisclosure(unittest.TestCase):
    """披露層與檢索／回原文共用同一條規則（`runtime/disclosure.disclosable`）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.folder = self.root / "prethink"
        self.folder.mkdir(parents=True)
        self.path = self.folder / "mira.json"

    def write_note(self, room, note="客人那段的預想"):
        self.path.write_text(json.dumps({"at": time.time(), "note": note, "room": room},
                                        ensure_ascii=False), encoding="utf-8")

    def thinker(self):
        return PreThinker(raw_log=RawLog(self.root / "raw"), call=lambda prompt: "",
                          root=self.folder, entities=["mira"], idle_s=60, interval_s=30)

    def test_guest_room_note_is_not_given_to_owner(self):
        self.write_note(["mira", "nori"])
        thinker = self.thinker()
        self.assertEqual(thinker.take("mira", present={"owner"}), "")
        self.assertEqual(thinker.peek("mira", present={"owner"}), "")
        # 錯的人不可以把它吃掉：對的人來還是拿得到
        self.assertEqual(thinker.take("mira", present={"nori"}), "客人那段的預想")

    def test_note_without_room_is_fail_closed(self):
        self.write_note(None)
        self.assertEqual(self.thinker().peek("mira", present={"owner"}), "")

    def test_present_subset_of_room_passes(self):
        self.write_note(["mira", "owner", "nori"])
        self.assertIn("預想", self.thinker().peek("mira", present={"owner"}))


class Raw:
    """只給 `recent()` 的原文倉替身（模擬用；時間全由測試控制）。"""

    def __init__(self, rows):
        self.rows = rows

    def recent(self, entity, limit=12):
        return self.rows[-limit:]


def talk(at, *, thread="th1", turns=("t1", "t2")):
    return [{"at": at, "thread": thread, "turn": turns[0], "role": "user", "speaker": "owner",
             "text": "今日は寒いね", "room": ["mira", "owner"]},
            {"at": at + 5, "thread": thread, "turn": turns[1], "role": "assistant",
             "speaker": "mira", "text": "ほんとに、手が冷たい", "room": ["mira", "owner"]}]


class Sim(unittest.TestCase):
    T0 = 1_800_000_000.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [self.T0]
        self.prompts = []

    def thinker(self, rows, **kwargs):
        return PreThinker(raw_log=Raw(rows), root=pathlib.Path(self.tmp.name) / "pt",
                          call=lambda prompt: self.prompts.append(prompt) or "さっきの、気になる",
                          entities=["mira"], idle_s=150, interval_s=120, max_age_s=1800,
                          clock=lambda: self.now[0], **kwargs)

    def run_minutes(self, thinker, minutes):
        start = self.now[0]
        for minute in range(minutes):
            self.now[0] = start + 60 * minute
            thinker.think_once("mira")


class TestPreThinkCost(Sim):
    def test_same_material_is_thought_once_over_a_quiet_night(self):
        rows = talk(self.T0)
        thinker = self.thinker(rows)
        self.run_minutes(thinker, 8 * 60)
        self.assertEqual(len(self.prompts), 1, "同一段話只想一次（以前 8 小時 239 次）")

    def test_new_turns_are_new_material(self):
        rows = talk(self.T0)
        thinker = self.thinker(rows)
        self.run_minutes(thinker, 10)
        rows.extend(talk(self.now[0] + 60, turns=("t3", "t4")))
        self.now[0] += 60 * 5
        self.run_minutes(thinker, 10)
        self.assertEqual(len(self.prompts), 2, "有新的一輪 ⇒ 再想一次")

    def test_taking_the_note_does_not_invite_a_rethink(self):
        thinker = self.thinker(talk(self.T0))
        self.run_minutes(thinker, 5)
        self.assertTrue(thinker.take("mira", present={"owner"}))
        self.run_minutes(thinker, 30)
        self.assertEqual(len(self.prompts), 1, "用掉了也不拿同一段再想")

    def test_asleep_and_no_budget_do_not_think(self):
        class Broke:
            def allows(self, entity, cost=1.0):
                return False

        for kwargs, reason in (({"asleep": lambda who, now: True}, "asleep"),
                               ({"budget": Broke()}, "no_budget")):
            thinker = self.thinker(talk(self.T0), **kwargs)
            self.now[0] = self.T0 + 600
            self.assertEqual(thinker.think_once("mira")["reason"], reason)
        self.assertEqual(self.prompts, [])


class TestPreThinkFreshness(Sim):
    def test_old_conversation_is_not_thought_about_as_just_now(self):
        thinker = self.thinker(talk(self.T0))
        self.now[0] = self.T0 + 10 * 3600            # 第二天早上
        result = thinker.think_once("mira")
        self.assertFalse(result["ok"])
        self.assertEqual(self.prompts, [], "十小時前的對話不拿來當「さっき」想")

    def test_note_ages_with_its_material_not_with_when_it_was_written(self):
        thinker = self.thinker(talk(self.T0))
        path = pathlib.Path(self.tmp.name) / "pt" / "mira.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"at": self.T0 + 9 * 3600, "material_at": self.T0,
                                    "note": "昨夜のこと", "room": ["mira", "owner"]},
                                   ensure_ascii=False), encoding="utf-8")
        self.now[0] = self.T0 + 9 * 3600 + 60
        self.assertEqual(thinker.peek("mira", present={"owner"}), "",
                         "剛寫的、但素材是昨晚的 ⇒ 不新鮮")


class TestPreThinkMaterial(Sim):
    def test_transcript_names_who_is_who(self):
        thinker = self.thinker(talk(self.T0))
        self.now[0] = self.T0 + 600
        self.assertTrue(thinker.think_once("mira")["ok"])
        transcript = self.prompts[0].split("ここ最近のやりとり：")[1]
        self.assertIn("オーナー: 今日は寒いね", transcript)
        self.assertIn("わたし: ほんとに", transcript)
        self.assertNotIn("owner:", transcript)
        self.assertNotIn("assistant:", transcript)

    def test_peer_dm_is_not_her_own_material(self):
        """角色之間的私聊（`kind=social`）是兩人的房間——不拿來想、不端給主人。"""
        from agent.brain.runtime.activity_log import ActivityLog
        log = ActivityLog(pathlib.Path(self.tmp.name) / "logs")
        log.append("mira", kind="social", visibility="private", text="ノリと私聊：内緒の話だよ")
        log.append("mira", kind="social", visibility="private", text="ノリと私聊：誰にも言わないで")
        log.append("mira", kind="study", visibility="public", text="規則書を三ページ読んだ")
        thinker = PreThinker(raw_log=None, root=pathlib.Path(self.tmp.name) / "pt2",
                             call=lambda prompt: self.prompts.append(prompt) or "x",
                             entities=["mira"], activity_log=log)
        self.assertEqual(thinker.think_once("mira")["reason"], "no_material",
                         "剩下的自己的事不夠兩件 ⇒ 不想")
        self.assertFalse(any("内緒" in prompt for prompt in self.prompts))
        log.append("mira", kind="study", visibility="public", text="判定を二つ整理した")
        self.assertTrue(thinker.think_once("mira")["ok"])
        self.assertNotIn("内緒", self.prompts[-1])
        self.assertIn("規則書", self.prompts[-1])


class TestStageInjection(unittest.TestCase):
    def test_stage_carries_prethink_note(self):
        from agent.brain.compose.stages import SubAgentStage

        class FakePre:
            def take(self, entity, *, present):
                return "（預想）他可能在等我把話接完。"

        stage = SubAgentStage(orchestrator=None, prethinker=FakePre())

        class Env:
            class decision:
                scope = type("S", (), {"entity": "mira"})()
                principal = "owner"

            text = "続き"
            meta = {}
            event = type("E", (), {"participants": {"owner"}})()

        class Ctx:
            def emit(self, *_args, **_kwargs):
                return None

        stage.run(Env(), Ctx())
        self.assertIn("[prethink]", Env.meta["subagent_notes"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
