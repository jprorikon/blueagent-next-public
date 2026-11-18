#!/usr/bin/env python3
"""主動搭話的時間基準、TRPG／現實的區分——每一條修法一個判據（2026-11-10 取證、2026-11-11 全數落地）。

  A1 主動消息的組句知道**現在幾點、她醒著沒**（以前只有「N 分前」，上午十點寫「夜が冷える」）；
  A2 窗口摘要帶「多久之前」（摘要裡的「今夜」不再等於現在）；
  B1 raw 行帶 `purpose`（分得出這一行是不是遊戲桌）；
  B2 「主人最近說過的話」不收 TRPG 那幾條線／那幾輪（資料形狀上是 role=user／speaker=owner）；
  §2.3 組句的退路（活線 transcript）也只取主人的話——角色互聊的「……う」不再寫成主人說的；
       角色之間的私聊進私記時只留「跟誰聊過」，不留對方原話；
  c16b418 一桌多席：桌階段事件看**那一席自己的**桌號與角色；沒登記的席位不交給預設席。
  B3／B4 記憶分場景（`game` 欄、蒸餾打標、召回過濾）另見 `test_memory_game_scene.py`；
  搭話改成危險率（日均 20 條）見 `test_proactive_hazard.py`。
"""

from __future__ import annotations

import pathlib
import tempfile
import time
import unittest
from types import SimpleNamespace

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.heartbeat_compose import make_heartbeat_composer, now_line
from agent.brain.domain.thread import ThreadKey, TurnRecord
from agent.brain.runtime.activity_log import ActivityLog
from agent.brain.runtime.memory.window_store import window_age
from agent.brain.runtime.rawlog import RawLog

MORNING = time.mktime((2026, 11, 10, 10, 42, 0, 0, 0, -1))


class Clock:
    def __init__(self, state: str) -> None:
        self.value = state

    def state(self, now=None) -> str:
        return self.value


class Brain:
    request = None

    def run_turn(self, request):
        Brain.request = request
        yield "done", {"text": "おはよう。"}


class TestNowLine(unittest.TestCase):
    def test_morning_and_awake(self) -> None:
        line = now_line("aster", SimpleNamespace(circadian={"aster": Clock("awake")}), now=MORNING)
        self.assertIn("10:42", line)
        self.assertIn("午前", line)
        self.assertIn("もう起きている", line)

    def test_without_a_clock_still_tells_the_time(self) -> None:
        self.assertIn("10:42", now_line("aster", None, now=MORNING))

    def test_composer_prompt_starts_from_now(self) -> None:
        compose = make_heartbeat_composer(
            brain=Brain(), souls=None, persona_cores={}, activity_log=None, affect=None,
            prethinker=None, proactive=SimpleNamespace(recent_texts=lambda entity, limit=3: [],
                                                       circadian={"aster": Clock("awake")}),
            raw_log=None, principals=SimpleNamespace(default=lambda: "owner"), speak_lang="ja",
            latest_window=lambda entity: "（10時間前・23:10ごろの会話）星の話：今夜は雲が多い")
        compose("aster", {"label": "ふつう"})
        text = Brain.request.text
        self.assertIn("いま：", text)
        self.assertIn("もう起きている", text)
        self.assertIn("いま」の時刻に合わせて書く", text)
        self.assertIn("10時間前", text, "窗口摘要帶著多久之前")


class TestWindowAge(unittest.TestCase):
    def test_age_words(self) -> None:
        now = MORNING
        self.assertEqual(window_age({"ended_at": now - 30}, now=now), "さっき")
        self.assertEqual(window_age({"ended_at": now - 25 * 60}, now=now), "25分前")
        self.assertTrue(window_age({"ended_at": now - 8 * 3600}, now=now).startswith("8時間前"))
        self.assertIn("月", window_age({"ended_at": now - 3 * 86400}, now=now))
        self.assertEqual(window_age({"ended_at": 0}, now=now), "")


class FakeThread:
    def __init__(self, entity, purpose, turns, thread_id="th-1", participants=("owner",)):
        self.key = ThreadKey(scope="owner", entity=entity, scene="together", purpose=purpose)
        self.id = thread_id
        self.participants = set(participants)
        self.segment = list(turns)
        self.transcript = list(turns)


def turn(turn_id, text, *, at, source="app:owner", speaker="owner", role="user"):
    return TurnRecord(turn_id=turn_id, role=role, text=text, source=source, at=at, speaker=speaker)


class TestRawPurpose(unittest.TestCase):
    def test_raw_rows_carry_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = RawLog(pathlib.Path(tmp) / "raw")
            now = time.time()
            raw.sync_thread(FakeThread("mira", "trpg", [
                turn("t1", "（あなたは今 TRPG を遊んでいる）", at=now, source="trpg:table-1")]))
            rows = raw.recent("mira")
            self.assertEqual(rows[-1].get("purpose"), "trpg")


class TestOwnerLinesSkipTheTable(unittest.TestCase):
    def test_trpg_threads_and_turns_are_not_owner_words(self) -> None:
        from agent.brain.runtime.threadstore import ThreadStore
        store = ThreadStore(path=pathlib.Path(tempfile.mkdtemp()) / "threads.json")
        now = time.time()
        chat = FakeThread("mira", "chat", [turn("c1", "ただいま", at=now - 600)], thread_id="th-chat")
        table = FakeThread("mira", "trpg", [
            turn("g1", "（盤面を見て、この手を決めて）", at=now - 60, source="trpg:table-1")],
            thread_id="th-trpg")
        stray = FakeThread("mira", "chat", [
            turn("g2", "（卓から起こされた）", at=now - 30, source="trpg:table-1")], thread_id="th-x")
        store._threads = {"a": chat, "b": table, "c": stray}
        lines = [row["text"] for row in store.recent_owner_lines("mira", 5)]
        self.assertEqual(lines, ["ただいま"])


class TestComposerFallback(unittest.TestCase):
    def test_peer_lines_are_not_written_as_owner_words(self) -> None:
        peer = [{"role": "user", "speaker": "aster", "text": "……う", "source": "app:cnv_1"},
                {"role": "assistant", "speaker": "mira", "text": "うん", "source": "app:cnv_1"}]
        compose = make_heartbeat_composer(
            brain=Brain(), souls=None, persona_cores={}, activity_log=None, affect=None,
            prethinker=None, proactive=SimpleNamespace(recent_texts=lambda entity, limit=3: []),
            raw_log=None, principals=SimpleNamespace(default=lambda: "owner"), speak_lang="ja",
            latest_window=lambda entity: "",
            recent_dialogue=lambda entity, limit=12: peer,
            recent_owner_lines=lambda entity, limit=3: [])
        compose("mira", {"label": "ふつう"})
        self.assertNotIn("……う", Brain.request.text, "角色互聊的話不是主人說的")
        self.assertNotIn("主人が最後に言っていたこと", Brain.request.text)


class TestPeerDmInPrivateLog(unittest.TestCase):
    def test_private_tail_keeps_who_but_not_what(self) -> None:
        log = ActivityLog(pathlib.Path(tempfile.mkdtemp()) / "logs")
        log.append("mira", kind="social", visibility="private", text="アスターと私聊：内緒の話だよ")
        log.append("mira", kind="rest", visibility="private", text="少し休む")
        tail = log.private_tail("mira")
        self.assertEqual(tail[0], "アスターと二人で話した（中身は二人だけのこと）")
        self.assertEqual(tail[1], "少し休む")
        self.assertFalse(any("内緒" in line for line in tail))


class TestMultiSeat(unittest.TestCase):
    def test_phase_follows_that_seats_own_table_and_entity(self) -> None:
        from agent.adapter.trpg import TrpgConfig
        from agent.server.integrations.trpg import make_phase_notifier
        published = []
        events = SimpleNamespace(publish=lambda kind, text, **kw: published.append(kw))
        state = SimpleNamespace(app=SimpleNamespace(world_events=events))
        cfg = TrpgConfig(seat="p2", entity="aster")
        phase = make_phase_notifier(state, cfg)
        phase("round", 2)
        self.assertEqual(published, [], "還沒坐上桌就不投")
        cfg.table_id = "t9"                      # kick 換桌時改的是這一席自己的 cfg
        phase("round", 3)
        self.assertEqual((published[-1]["table_id"], published[-1]["entities"]), ("t9", ("aster",)))

    def test_unknown_seat_is_not_handed_to_the_default_seat(self) -> None:
        from agent.server.integrations.trpg import pick_seat
        first, second = object(), object()
        app = SimpleNamespace(trpg_seats={"p1": first, "p2": second}, trpg=first)
        self.assertEqual(pick_seat(app, {"seat": "p2"}), (second, ""))
        self.assertEqual(pick_seat(app, {}), (first, ""))
        self.assertEqual(pick_seat(app, {"seat": "p7"}), (first, "p7"), "多席時未知席位要回 404")
        single = SimpleNamespace(trpg_seats={"p1": first}, trpg=first)
        self.assertEqual(pick_seat(single, {"seat": "p7"}), (first, ""), "單席照舊跟著 kick 換席")


if __name__ == "__main__":
    unittest.main(verbosity=2)
