"""跨線短期餘溫（D2，owner 2026-11-17：「帶上餘溫，但是需要標註渠道來源」）＋ `[[before]]` 回歸。

判據：
  ① 真的跑兩輪：Discord 私聊剛聊完、轉到 App ⇒ App 那一輪的 prompt 有 `[[elsewhere]]`，
     寫著 Discord 那邊的話，並標了「Discord の個別チャット」；
  ② 披露：這一場有人沒參與那段（客人在場）⇒ 不給；
  ③ 只看 15 分鐘內；她自己的線、桌（trpg）不算；
  ④ 渠道標籤：App 私聊／App 群組／通話／Discord 語音分得出來；
  ⑤ `[[before]]`：線上的承接摘要真的進到 prompt（2026-10-25 起 `build_request` 沒帶它）。
"""

from __future__ import annotations

import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.ids import ThreadKey
from agent.brain.domain.thread import Thread, TurnRecord
from agent.brain.domain.event import SourceClass
from agent.brain.messages import build_messages
from agent.brain.messages_continuity import channel_label
from agent.brain.ports import BrainRequest
from agent.brain.runtime.threadstore import ThreadStore


class _Capture:
    def __init__(self):
        self.requests = []

    def run_turn(self, request):
        self.requests.append(request)
        yield ("delta", {"text": "うん。"})
        yield ("done", {"text": "うん。", "entity": request.entity, "principal": request.principal,
                        "model": "test"})


def _body(request) -> str:
    return "\n".join(str(item.get("content") or "") for item in build_messages(request))


class TestRealTurns(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.brain = _Capture()
        self.state.app.runtime.ports.brain = self.brain

    def test_group_talk_reaches_the_direct_turn_with_its_channel(self):
        # 一對一的 App／Discord 本來就共用一條線（7.6）；斷開的是群組／語音 ↔ 私聊
        self.state.send("池袋の電器店に寄ってから帰るね", entity="mira", principal="owner",
                        participants=("owner",), group=True, mention=True,
                        source_override="app:cnv_group", scope_override="group:cnv_group",
                        scene_override="app-group")
        self.state.send("ただいま", entity="mira", principal="owner", source_override="phone:owner")
        first, second = self.brain.requests[0], self.brain.requests[-1]
        self.assertNotEqual(first.thread_id, second.thread_id, "群組與私聊要是兩條線，這個測試才有意義")
        sections = second.meta.get("elsewhere") or []
        self.assertEqual(len(sections), 1, sections)
        self.assertTrue(sections[0]["group"])
        body = _body(second)
        self.assertIn("[[elsewhere]]", body)
        self.assertIn("池袋の電器店", body)
        self.assertIn("App のグループ", body)
        # 同一條線自己的話不重複進餘溫
        self.assertNotIn("ただいま", "\n".join(row["text"] for row in sections[0]["rows"]))

    def test_app_group_turn_uses_the_one_liner_form(self):
        """M5 §10：App 群聊回合送進大腦的形是「ひと言」，私聊照舊三句（2026-11-17）。"""
        self.state.send("今日なに食べた？", entity="mira", principal="owner",
                        participants=("owner",), group=True, mention=True,
                        source_override="app:cnv_group", scope_override="group:cnv_group",
                        scene_override="app-group")
        self.assertIn("グループの雑談", _body(self.brain.requests[-1]))
        self.state.send("ただいま", entity="mira", principal="owner", source_override="phone:owner")
        direct = _body(self.brain.requests[-1])
        self.assertNotIn("グループの雑談", direct)
        self.assertIn("全体で 1〜3 文", direct)

    def test_someone_who_was_not_there_blocks_it(self):
        self.state.send("主人だけに話すこと", entity="mira", principal="owner",
                        source_override="discord:dm")
        self.state.send("みんなでこんにちは", entity="mira", principal="owner",
                        participants=("owner", "alice"), group=True, mention=True,
                        source_override="app:cnv_group", scope_override="group:cnv_group",
                        scene_override="app-group")
        last = self.brain.requests[-1]
        self.assertEqual(last.meta.get("elsewhere") or [], [])
        self.assertNotIn("主人だけに話すこと", _body(last))

    def test_carry_summary_reaches_the_prompt(self):
        self.state.send("おはよう", entity="mira", principal="owner", source_override="phone:owner")
        thread = self.state.app.runtime.threads.get(self.brain.requests[-1].thread_id)
        thread.carry_summary = "前に小説の第六巻の話をした"
        self.state.send("続きの話", entity="mira", principal="owner", source_override="phone:owner")
        last = self.brain.requests[-1]
        self.assertEqual(last.meta.get("carry_summary"), "前に小説の第六巻の話をした")
        self.assertIn("[[before]]", _body(last))

    def test_new_generation_carries_the_tail_of_the_last_one(self):
        """過窗換世代：舊線最後那一段先封片，新世代的 [[before]] 接得到它（2026-11-17）。"""
        self.state.send("池袋の電器店でケーブルを買った", entity="mira", principal="owner",
                        source_override="phone:owner")
        old = self.state.app.runtime.threads.get(self.brain.requests[-1].thread_id)
        self.assertTrue(old.segment, "第一段還沒封片，這個測試才有意義")
        with old.lock:
            old.last_active_at -= 20 * 60                 # 閒過 INTERACTIVE 的 15 分鐘窗
        self.state.send("ただいま", entity="mira", principal="owner", source_override="phone:owner")
        last = self.brain.requests[-1]
        self.assertNotEqual(last.thread_id, old.id, "要真的換世代")
        self.assertIn("ケーブル", last.meta.get("carry_summary") or "")
        self.assertIn("[[before]]", _body(last))
        self.assertEqual(old.segment, [], "舊線的尾巴封掉了")


def _thread(scope, entity, purpose, turns, *, thread_id, participants=("owner",)):
    key = ThreadKey(scope, entity, purpose=purpose)
    thread = Thread(key=key, id=thread_id, lane=SourceClass.INTERACTIVE,
                    created_at=0.0, last_active_at=max([t.at for t in turns] or [0.0]))
    thread.participants.update(participants)
    for item in turns:
        thread.record(item)
    return thread


def _turn(text, at, *, source="discord:dm", role="user", speaker="owner"):
    return TurnRecord(turn_id=f"t{at}", role=role, text=text, source=source, at=at, speaker=speaker)


class TestStoreRules(unittest.TestCase):
    def setUp(self):
        self.store = ThreadStore()
        self.now = time.time()

    def test_window_self_and_table_are_skipped(self):
        now = self.now
        self.store._threads = {
            "old": _thread("owner", "aster", "chat", [_turn("昔の話", now - 3600)], thread_id="old"),
            "self": _thread("self:aster", "aster", "chat", [_turn("独り言", now - 60)], thread_id="self"),
            "trpg": _thread("owner", "aster", "trpg", [_turn("卓の話", now - 60)], thread_id="trpg"),
            "clock": _thread("owner", "aster", "chat", [_turn("時報", now - 60, source="clock:tick")],
                             thread_id="clock"),
            "ok": _thread("owner", "aster", "chat", [
                _turn("一", now - 300), _turn("二", now - 200),
                _turn("三", now - 100, role="assistant", speaker="aster"), _turn("四", now - 50)],
                thread_id="ok"),
        }
        found = self.store.elsewhere("aster", current="here", present=("owner", "aster"), now=now)
        self.assertEqual([item["rows"][-1]["text"] for item in found], ["四"])
        self.assertEqual([row["text"] for row in found[0]["rows"]], ["二", "三", "四"], "每段最多三輪")

    def test_present_must_be_a_subset_of_that_room(self):
        now = self.now
        self.store._threads = {"dm": _thread("owner", "aster", "chat", [_turn("内緒", now - 60)],
                                             thread_id="dm")}
        self.assertEqual(self.store.elsewhere("aster", current="x", present=("owner", "guest-1"), now=now), [])
        self.assertEqual(len(self.store.elsewhere("aster", current="x", present=("owner",), now=now)), 1)

    def test_newest_two_threads_only(self):
        now = self.now
        self.store._threads = {
            name: _thread("owner", "aster", "chat", [_turn(name, now - age)], thread_id=name)
            for name, age in (("a", 300), ("b", 200), ("c", 100))}
        found = self.store.elsewhere("aster", current="x", present=("owner",), now=now)
        self.assertEqual([item["rows"][0]["text"] for item in found], ["c", "b"])


class TestLabels(unittest.TestCase):
    def test_each_channel_is_named(self):
        self.assertEqual(channel_label("phone:owner", group=False), "App の個別チャット")
        self.assertEqual(channel_label("app:cnv_1", group=True), "App のグループ")
        self.assertEqual(channel_label("phone:call", group=False), "App の通話")
        self.assertEqual(channel_label("discord:vc:123", group=True), "Discord のボイスチャンネル")
        self.assertEqual(channel_label("discord:dm", group=False), "Discord の個別チャット")

    def test_block_names_speakers_and_her(self):
        now = time.time()
        request = BrainRequest(thread_id="th", entity="aster", principal="owner", text="ただいま", system="",
                               meta={"now": now, "names": {"owner": "オーナー"}, "entity_name": "アスター",
                                     "elsewhere": [{"source": "discord:vc:1", "group": True, "at": now - 120,
                                                    "rows": [{"role": "user", "speaker": "owner", "text": "聞こえる？"},
                                                             {"role": "assistant", "speaker": "aster", "text": "聞こえるよ"}]}]})
        body = _body(request)
        self.assertIn("〔Discord のボイスチャンネル・2 分前〕", body)
        self.assertIn("- オーナー：聞こえる？", body)
        self.assertIn("- あなた：聞こえるよ", body)

    def test_a_shared_direct_thread_relabels_when_the_channel_changes(self):
        now = time.time()
        request = BrainRequest(thread_id="th", entity="aster", principal="owner", text="?", system="",
                               meta={"now": now, "names": {"owner": "オーナー"},
                                     "elsewhere": [{"source": "phone:owner", "group": False, "at": now - 60,
                                                    "rows": [{"role": "user", "speaker": "owner", "text": "A",
                                                              "source": "discord:dm", "at": now - 300},
                                                             {"role": "user", "speaker": "owner", "text": "B",
                                                              "source": "phone:owner", "at": now - 60}]}]})
        body = _body(request)
        self.assertLess(body.index("〔Discord の個別チャット・5 分前〕"), body.index("- オーナー：A"))
        self.assertLess(body.index("- オーナー：A"), body.index("〔App の個別チャット・さっき〕"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
