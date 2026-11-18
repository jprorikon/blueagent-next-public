#!/usr/bin/env python3
"""披露層回歸（2026-10-24 review）：**在場的人 ⊆ 房間**，三層＋承接段＋心跳都一樣。

判據（每條對應 review 的一個洞）：
  ① 原文／窗口層方向不能反：主人＋alice 同時在場時，主人 1:1 的原文與窗口都不回；
     `RawLog.search(room=…)` 同一條規則；窗口找不到原文 ⇒ 不說（fail-closed）；
  ② `memory.search` 工具帶整個房間：群聊裡主人發言時也不能查出主人 1:1；
  ③ 承接段只接同一條線：同時段別人那條線的窗口不能當 `[[before]]`；
  ④ 心跳接地只拿主人聽得的窗口：客人那條線再新也不拿；
  ⑤ 副通道：遠端沒 key ⇒ 不建呼叫（mock 模式不外呼）；本機端點不用 key；
  ⑥ 提前想：只回放最近那一條線，預想帶房間；房間對不上的人拿不到（也不會把它用掉）。
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.domain.thread import ThreadKey, TurnRecord
from agent.brain.runtime.distill import _SUMMARY_SCHEMA
from agent.brain.runtime.memory.memory_search import MemorySearch
from agent.brain.runtime.rawlog import RawLog
from agent.brain.runtime.tools import MemorySearchTool, ToolContext

OWNER_LINE = ThreadKey(scope="owner", entity="mira", scene="chat", purpose="chat")
GUEST_LINE = ThreadKey(scope="alice", entity="mira", scene="chat", purpose="chat")
SECRET = "健康診断の結果がちょっと悪かった"


class FakeThread:
    def __init__(self, key, generation, participants, turns):
        self.key = key
        self.id = key.with_generation(generation)
        self.participants = set(participants)
        self.segment = list(turns)


def turn(turn_id, text, *, at, speaker):
    return TurnRecord(turn_id=turn_id, role="user", text=text, source="app", at=at,
                      speaker=speaker)


class LeakCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.db = root / "facts.db"
        self.memory = SqliteMemory(self.db, seed=False)
        self.raw = RawLog(root / "raw")
        self.t0 = time.time() - 3600
        # 主人 1:1（房間 {她, owner}）與 alice 1:1（房間 {她, alice}）同一時段各說了一句
        self.raw.sync_thread(FakeThread(OWNER_LINE, 1, {"owner"}, [
            turn("t1", f"秘密：{SECRET}", at=self.t0, speaker="owner")]))
        self.raw.sync_thread(FakeThread(GUEST_LINE, 1, {"alice"}, [
            turn("t1", "アリスの話：来週引っ越しする", at=self.t0 + 30, speaker="alice")]))
        self.memory.add(f"主人の{SECRET}", layer="pair",
                        audience={"mira", "owner"}, entity="mira")
        db = sqlite3.connect(self.db)
        db.executescript(_SUMMARY_SCHEMA)
        db.execute("INSERT INTO window_summaries (window_id, entity, thread, started_at,"
                   " ended_at, turns, topic, summary, people) VALUES (?,?,?,?,?,?,?,?,?)",
                   ("w_owner", "mira", OWNER_LINE.with_generation(1), self.t0, self.t0 + 60,
                    8, "健康診断", f"主人が{SECRET}と打ち明けた。", "[]"))
        db.execute("INSERT INTO window_summaries (window_id, entity, thread, started_at,"
                   " ended_at, turns, topic, summary, people) VALUES (?,?,?,?,?,?,?,?,?)",
                   ("w_guest", "mira", GUEST_LINE.with_generation(1), self.t0 + 30,
                    self.t0 + 90, 8, "引っ越し", "アリスが来週引っ越すと話した。", "[]"))
        db.commit()
        db.close()
        self.search = MemorySearch(memory=self.memory, raw_log=self.raw, db_path=self.db)

    def _texts(self, out):
        return ([t["text"] for t in out["turns"]] + [w["summary"] for w in out["windows"]]
                + [f["text"] for f in out["facts"]])


class TestTurnAndWindowDirection(LeakCase):
    def test_owner_alone_recalls_own_room(self):
        out = self.search.search(entity="mira", present={"owner"}, query=SECRET, limit=5)
        self.assertTrue(any(SECRET in t["text"] for t in out["turns"]), out)
        self.assertTrue(any(SECRET in w["summary"] for w in out["windows"]), out)

    def test_extra_person_in_room_blocks_owner_one_on_one(self):
        out = self.search.search(entity="mira", present={"owner", "alice"},
                                 query=SECRET, limit=5)
        self.assertFalse([text for text in self._texts(out) if SECRET in text], out)

    def test_rawlog_search_uses_same_direction(self):
        self.assertTrue(self.raw.search("mira", SECRET, room={"mira", "owner"}))
        self.assertEqual(self.raw.search("mira", SECRET,
                                         room={"mira", "owner", "alice"}), [])

    def test_window_without_raw_rows_is_not_disclosed(self):
        db = sqlite3.connect(self.db)
        db.execute("INSERT INTO window_summaries (window_id, entity, thread, started_at,"
                   " ended_at, turns, topic, summary, people) VALUES (?,?,?,?,?,?,?,?,?)",
                   ("w_orphan", "mira", "th_gone-1", self.t0, self.t0 + 60, 8,
                    "健康診断 孤児", f"出所不明：{SECRET}", "[]"))
        db.commit()
        db.close()
        out = self.search.search(entity="mira", present={"owner"}, query=SECRET, limit=10,
                                 layers=("window",))
        self.assertNotIn("w_orphan", [w["window_id"] for w in out["windows"]], out)


class TestToolCarriesWholeRoom(LeakCase):
    def test_group_turn_does_not_search_as_one_on_one(self):
        tool = MemorySearchTool(self.search)
        ctx = ToolContext(entity="mira", principal="owner", thread_id="th-group",
                          source="discord", voice=None,
                          present=frozenset({"owner", "alice"}))
        result = tool.run(ctx, {"query": SECRET, "limit": 5})
        self.assertTrue(result.ok, result.error)
        self.assertFalse([text for text in self._texts(result.data) if SECRET in text],
                         result.data)

    def test_one_on_one_still_works(self):
        tool = MemorySearchTool(self.search)
        ctx = ToolContext(entity="mira", principal="owner", thread_id="th-1",
                          source="app", voice=None)
        result = tool.run(ctx, {"query": SECRET, "limit": 5})
        self.assertTrue(result.ok, result.error)
        self.assertTrue([text for text in self._texts(result.data) if SECRET in text])


class TestCarryAndHeartbeat(LeakCase):
    def test_carry_only_takes_same_line(self):
        # alice 那條線封片：時段跟主人那個窗口重疊，但只能接到自己的
        carry = self.search.carry_window(GUEST_LINE, self.t0, self.t0 + 90)
        self.assertIn("引っ越", carry)
        self.assertNotIn(SECRET, carry)
        # 換世代也接得上（同一條線的 base）
        self.assertIn(SECRET, self.search.carry_window(OWNER_LINE, self.t0, self.t0 + 60))

    def test_carry_from_unrelated_line_is_empty(self):
        other = ThreadKey(scope="bob", entity="mira", scene="chat", purpose="chat")
        self.assertEqual(self.search.carry_window(other, self.t0, self.t0 + 90), "")

    def test_heartbeat_grounding_skips_guest_window(self):
        # 客人的窗口比較新，但主人聽不得 ⇒ 拿主人自己那條
        latest = self.search.latest_window("mira", {"owner"})
        self.assertIn(SECRET, latest)
        self.assertNotIn("引っ越", latest)
        self.assertIn("引っ越", self.search.latest_window("mira", {"alice"}))


class TestPreThinkStaysInRoom(LeakCase):
    def setUp(self):
        super().setUp()
        # 最近那條線：alice（預想至少要兩輪）
        self.raw.sync_thread(FakeThread(GUEST_LINE, 2, {"alice"}, [
            turn("t1", "引っ越しの準備が大変", at=self.t0 + 120, speaker="alice"),
            turn("t2", "段ボールが足りない", at=self.t0 + 150, speaker="alice")]))

    def thinker(self):
        from agent.brain.runtime.prethink import PreThinker
        self.prompts = []

        def call(prompt):
            self.prompts.append(prompt)
            return "（預想）さっきの話、もう少し聞きたい。"

        return PreThinker(raw_log=self.raw, call=call, root=pathlib.Path(self.tmp.name) / "pt",
                          entities=["mira"], idle_s=60, interval_s=30, max_age_s=7200)

    def test_only_replays_the_latest_line(self):
        thinker = self.thinker()
        self.assertTrue(thinker.think_once("mira")["ok"])
        # 最近那條是 alice 的線；主人 1:1 的秘密不該被一起回放
        self.assertNotIn(SECRET, self.prompts[0])
        self.assertIn("引っ越し", self.prompts[0])

    def test_note_only_released_to_its_room(self):
        thinker = self.thinker()
        thinker.think_once("mira")
        self.assertEqual(thinker.peek("mira", present={"owner"}), "", "心跳（主人）拿不到")
        self.assertEqual(thinker.take("mira", present={"owner"}), "")
        self.assertEqual(thinker.take("mira", present={"owner", "alice"}), "", "多一個人也不行")
        # 房間不對時沒有被用掉 ⇒ alice 回來還拿得到
        self.assertIn("預想", thinker.take("mira", present={"alice"}))

    def test_old_note_without_room_is_withheld(self):
        import json
        thinker = self.thinker()
        path = pathlib.Path(self.tmp.name) / "pt" / "mira.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"entity": "mira", "at": time.time(),
                                    "note": f"主人の{SECRET}が気になる"}, ensure_ascii=False),
                        encoding="utf-8")
        self.assertEqual(thinker.take("mira", present={"alice"}), "")
        self.assertEqual(thinker.peek("mira", present={"owner"}), "")

    def test_stage_passes_the_room(self):
        from agent.brain.compose.stages import SubAgentStage
        thinker = self.thinker()
        thinker.think_once("mira")
        stage = SubAgentStage(orchestrator=None, prethinker=thinker)

        def run(principal, participants):
            env = type("Env", (), {})()
            env.decision = type("D", (), {"principal": principal,
                                          "scope": type("S", (), {"entity": "mira"})()})()
            env.event = type("E", (), {"participants": participants})()
            env.text, env.meta = "やあ", {}
            stage.run(env, type("C", (), {"emit": lambda *a, **k: None})())
            return env.meta.get("subagent_notes") or []

        self.assertEqual(run("owner", {"owner"}), [])
        self.assertTrue(run("alice", {"alice"}))


class TestSideChannelsNeedKey(unittest.TestCase):
    NO_KEY = {"BLUEAGENT_LLM_API_KEY": "", "DEEPSEEK_API_KEY": "", "VISION_API_KEY": ""}

    def test_remote_without_key_is_not_built(self):
        from agent.brain.persona.core import http_call
        self.assertIsNone(http_call({"BLUEAGENT_LLM_BASE_URL": "https://api.deepseek.com/v1"}))
        self.assertIsNone(http_call({}))

    def test_local_or_keyed_endpoints_are_built(self):
        from agent.brain.persona.core import http_call
        self.assertTrue(callable(http_call({"BLUEAGENT_LLM_BASE_URL": "http://127.0.0.1:11435/v1"})))
        self.assertTrue(callable(http_call({"BLUEAGENT_LLM_BASE_URL": "https://api.deepseek.com/v1",
                                            "DEEPSEEK_API_KEY": "sk-test"})))

    def test_vision_does_not_call_remote_without_key(self):
        from agent.brain.media import vision
        with mock.patch.object(vision, "VISION_KEY", ""), \
                mock.patch.object(vision, "_role", return_value={
                    "base_url": "https://api.deepseek.com/v1", "api_key": "", "model": "m"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=OSError("不該外呼")) as urlopen:
            self.assertEqual(vision.describe(b"\x89PNG"), "")
        urlopen.assert_not_called()             # describe() 會吞例外，所以要看有沒有真的呼叫

    def test_mock_build_has_no_remote_side_calls(self):
        env = {key: value for key, value in os.environ.items()
               if key not in {"BLUEAGENT_LLM_BASE_URL", "BLUEAGENT_LLM_MODEL",
                              "BLUEAGENT_BRAIN", "BLUEAGENT_STATE_LLM_BASE_URL"}}
        env.update(self.NO_KEY)
        env["BLUEAGENT_BRAIN"] = "mock"            # 沒有預設大腦了：mock 要明寫
        root = tempfile.mkdtemp(prefix="ba-nokey-")
        with mock.patch.dict(os.environ, env, clear=True):
            demo = build(brain_delay=0, data_dir=root, step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        reranker = getattr(demo, "reranker", None)
        self.assertTrue(reranker is None or reranker.call is None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
