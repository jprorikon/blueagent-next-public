#!/usr/bin/env python3
"""三人房的定时敲门（GROUP-SELF-CHAT §2 ①；后台报告「没人说话时不会自己醒来敲门」）。

判据：
  ① 不到点不敲；敲了没拿起手机 ⇒ 什么都没发生（不拉消息、不回合）；
  ② 拿起了、房里有她没看过的 ⇒ 对最新那则走正常群聊回合（占名额、照样发）；
  ③ 拿起了、房里安静 10 分钟以上 ⇒ 问她要不要开场；说了才发、并写回她的群聊线；
  ④ 她说不（多数情况）⇒ 什么都不发；一小时内不再问第二次；
  ⑤ 话还在进行（最后一句不到 10 分钟）⇒ 不插新话题；
  ⑥ 账本里还没有房 ⇒ 问一次 App Server 找到固定房；
  ⑦ `GroupOpener`：`（なし）` ⇒ 空字串；说出口的那句以 assistant 写回 `group:<id>` 的线。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import types
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter import app_group_delay
from agent.adapter.app_group import GroupLedger, OPENER_TRY_GAP_S, QUIET_S
from agent.adapter.app_group_knock import knock_tick
from agent.adapter.app_server import AppChatAdapter
from agent.brain.compose.group_opener import SILENT, GroupOpener, opener_prompt
from agent.brain.domain.ids import ThreadKey

NOW = 1_800_000_000.0
ROOM = "cnv_room"


class Client:
    def __init__(self, messages=(), conversations=None):
        self.messages = list(messages)
        self.conversations = conversations
        self.sent, self.read, self.calls = [], [], []

    def events(self, after, limit=100):
        return {"events": [], "next_cursor": after, "has_more": False}

    def board_call(self, name, arguments):
        self.calls.append(name)
        if name == "chat.messages":
            return {"messages": list(self.messages)[-int(arguments.get("limit") or 50):]}
        if name == "chat.conversations":
            return self.conversations or []
        raise AssertionError(name)

    def mark_read(self, conversation_id, message_id):
        self.read.append((conversation_id, message_id))

    def send_message(self, conversation_id, parts, *, mutation_id, causation_id):
        self.sent.append({"conversation_id": conversation_id, "parts": list(parts),
                          "mutation_id": mutation_id})
        return {"message": {"id": "sent"}}


class Attention:
    """拿起率由测试指定；`decision` 只给新消息路径用（敲门路径绕过它）。"""

    def __init__(self, p):
        self.p = p

    def probability(self, entity, at, **context):
        self.context = context
        return self.p, "notify"

    def decision(self, entity, message, **context):
        raise AssertionError("敲门路径不该再掷一次")


class Opener:
    def __init__(self, line=""):
        self.line, self.asked, self.recorded = line, [], []

    def compose(self, entity, lines, quiet_s):
        self.asked.append((entity, list(lines), quiet_s))
        return self.line

    def record(self, entity, conversation_id, text, at, participants=()):
        self.recorded.append((entity, conversation_id, text, list(participants)))
        return True


def message(mid, author, text, at, kind="entity"):
    return {"id": mid, "conversation_id": ROOM, "created_at": at,
            "author": {"id": author, "kind": kind}, "parts": [{"kind": "text", "text": text}]}


class KnockCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-knock-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.turns = []

    def adapter(self, client, *, p=1.0, opener=None, ledger=None, room=True):
        ledger = ledger or GroupLedger(self.root / "ledger.json", clock=lambda: NOW)
        if room:
            ledger.remember_room(ROOM, ["owner", "aster", "nori", "mira"])
        cursor = self.root / "aster-cursor.json"
        cursor.write_text(json.dumps({"cursor": "0"}), encoding="utf-8")

        def send(text, **kwargs):
            self.turns.append({"text": text, **kwargs})
            return {"frames": [{"kind": "done", "payload": {"text": "うん。", "bubbles": ["うん。"]}}]}
        return AppChatAdapter("aster", client, send, cursor, lambda r: r["frames"][-1]["payload"]["text"],
                              lambda r: r["frames"][-1]["payload"]["bubbles"],
                              group_attention=Attention(p), group_ledger=ledger,
                              opener=opener, clock=lambda: NOW,
                              names={"nori": "ノリ", "mira": "ミラ"})

    def test_waits_for_its_turn_and_unseen_knock_does_nothing(self):
        client = Client([message("m1", "nori", "ねえ", NOW - 30)])
        adapter = self.adapter(client, p=0.0, opener=Opener("x"))
        self.assertEqual(knock_tick(adapter, NOW), "not_seen")
        self.assertEqual((client.calls, self.turns, client.sent), ([], [], []))
        self.assertEqual(adapter.group_attention.context["notify"], False, "敲门没有新消息的通知")
        self.assertEqual(knock_tick(adapter, NOW + 60), "wait", "约 5 分钟才敲下一次")
        self.assertGreaterEqual(adapter._next_knock, NOW + 150)
        self.assertLessEqual(adapter._next_knock, NOW + 450)

    def test_catches_up_on_the_latest_unseen_message(self):
        client = Client([message("m1", "nori", "暇だね", NOW - 3600),
                         message("m2", "mira", "ねえ、聞いて", NOW - 1800)])
        adapter = self.adapter(client, opener=Opener("x"))
        self.assertEqual(knock_tick(adapter, NOW), "caught_up")
        self.assertEqual(self.turns, [], "补看也入队：回合由工作线程跑（2026-11-16）")
        self.assertEqual(app_group_delay.tick(adapter, NOW), 1)
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(self.turns[0]["text"], "ねえ、聞いて")
        self.assertEqual(self.turns[0]["addressed_to"], "", "拿到名额：由她自己决定讲不讲")
        self.assertEqual(self.turns[0]["participants"], ["aster", "mira", "nori", "owner"])
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.read, [(ROOM, "m2")])
        self.assertEqual(adapter.opener.asked, [], "有没看过的就不另开话题")

    def test_quiet_room_lets_her_open_a_topic_and_records_it(self):
        client = Client([message("m1", "owner", "おやすみ", NOW - 7200, kind="owner")])
        ledger = GroupLedger(self.root / "ledger.json", clock=lambda: NOW)
        ledger.mark_seen(ROOM, "aster", NOW - 7000)                  # 那句她早就看过
        opener = Opener("さっき本屋で面白い図鑑見つけた。誰か興味ある？")
        adapter = self.adapter(client, opener=opener, ledger=ledger)
        self.assertEqual(knock_tick(adapter, NOW), "opened")
        self.assertEqual(client.sent[0]["parts"], [opener.line])
        self.assertTrue(client.sent[0]["mutation_id"].startswith("agent-opener:aster:"))
        self.assertEqual(opener.asked[0][1], [("オーナー", "おやすみ")], "owner 在 prompt 里叫オーナー")
        self.assertEqual(opener.recorded[0][:3], ("aster", ROOM, opener.line), "写回她的群聊线")
        self.assertEqual(ledger._data["rooms"][ROOM]["turn_no"], 1, "开场也算一个 turn")

    def test_declining_is_the_usual_answer_and_she_is_not_asked_again_within_an_hour(self):
        client = Client([])
        opener = Opener("")
        adapter = self.adapter(client, opener=opener)
        self.assertEqual(knock_tick(adapter, NOW), "declined")
        self.assertEqual(client.sent, [])
        adapter._next_knock = 0.0
        self.assertEqual(knock_tick(adapter, NOW + 600), "opener_wait")
        self.assertEqual(len(opener.asked), 1)
        adapter._next_knock = 0.0
        self.assertEqual(knock_tick(adapter, NOW + OPENER_TRY_GAP_S + 1), "declined")

    def test_ongoing_conversation_is_not_interrupted(self):
        client = Client([message("m1", "nori", "それでね", NOW - 120)])
        ledger = GroupLedger(self.root / "ledger.json", clock=lambda: NOW)
        ledger.mark_seen(ROOM, "aster", NOW - 60)
        opener = Opener("x")
        self.assertEqual(knock_tick(self.adapter(client, opener=opener, ledger=ledger), NOW), "busy")
        self.assertEqual(opener.asked, [])
        self.assertLess(120, QUIET_S)

    def test_finds_the_room_from_the_app_server_when_the_ledger_is_empty(self):
        client = Client([], conversations=[
            {"id": "cnv_dm", "kind": "direct", "participants": ["owner", "aster"]},
            {"id": ROOM, "kind": "group", "participants": ["owner", "aster", "nori"]}])
        adapter = self.adapter(client, opener=Opener(""), room=False)
        self.assertEqual(knock_tick(adapter, NOW), "declined")
        self.assertEqual(adapter.group_ledger.rooms(), [ROOM])
        self.assertEqual(adapter.group_ledger.participants(ROOM), ["aster", "nori", "owner"])

    def test_pending_room_does_not_pause_other_rooms(self):
        ledger = GroupLedger(self.root / "room-scoped.json", clock=lambda: NOW)
        ledger.remember_room("cnv_a", ["owner", "aster", "nori"])
        client = Client([message("m-b", "nori", "別の部屋の新着", NOW - 30)])
        adapter = self.adapter(client, ledger=ledger)       # 再记入 ROOM，顺序是 A、B
        app_group_delay.hold(adapter, {}, conversation_id="cnv_a", message_id="pending-a",
                             at=NOW, delay=60.0, granted=False, reason="ok", from_ai=False)
        self.assertEqual(knock_tick(adapter, NOW), "caught_up")
        app_group_delay.tick(adapter, NOW)                  # B 的补看到期；A 还没到
        self.assertEqual(client.read, [(ROOM, "m-b")], "A 等待时仍要敲 B")
        self.assertEqual(app_group_delay.pending_rooms(adapter), {"cnv_a"})

    def test_all_pending_does_not_consume_knock_and_recovers_after_settlement(self):
        client = Client([])
        adapter = self.adapter(client, opener=Opener(""))
        app_group_delay.hold(adapter, {}, conversation_id=ROOM, message_id="pending-room",
                             at=NOW, delay=60.0, granted=False, reason="ok", from_ai=False)
        self.assertEqual(knock_tick(adapter, NOW), "pending")
        self.assertEqual(adapter._next_knock, 0.0, "全部暂停不能消耗下一次敲门机会")
        self.assertEqual(app_group_delay.tick(adapter, NOW + 61), 1)
        self.assertEqual(knock_tick(adapter, NOW + 61), "declined")
        self.assertGreater(adapter._next_knock, NOW + 61)


class OpenerCase(unittest.TestCase):
    def brain(self, text):
        return types.SimpleNamespace(run_turn=lambda request: iter([("done", {"text": text})]))

    def test_silence_and_clean_line(self):
        self.assertEqual(GroupOpener(brain=self.brain(SILENT), threads=None).compose("aster", [], 900), "")
        line = GroupOpener(brain=self.brain("「ちょっと聞いて」\n余計な行"), threads=None) \
            .compose("aster", [("ノリ", "暇")], 900)
        self.assertEqual(line, "ちょっと聞いて")

    def test_prompt_says_silence_is_the_default(self):
        text = opener_prompt([("オーナー", "おやすみ")], 1800)
        self.assertIn("30分", text)
        self.assertIn("たいていは何も書かない", text)
        self.assertIn(SILENT, text)

    def test_record_writes_her_line_into_the_group_thread(self):
        recorded = []
        thread = types.SimpleNamespace(lock=threading.Lock(), participants=set(),
                                       record=recorded.append, touch=lambda _at: None)
        keys = []

        def attach(key, **_kwargs):
            keys.append(key)
            return types.SimpleNamespace(thread=thread)
        opener = GroupOpener(brain=self.brain(""), threads=types.SimpleNamespace(attach=attach))
        self.assertTrue(opener.record("aster", ROOM, "ねえ", NOW, ["owner", "aster"]))
        same = ThreadKey(scope="group:" + ROOM, entity="aster", scene="app-group", purpose="chat")
        self.assertEqual(keys[0], same, "和正常群聊回合是同一条线（scope 会被规范化）")
        self.assertEqual((recorded[0].role, recorded[0].text, recorded[0].speaker),
                         ("assistant", "ねえ", "aster"))
        self.assertEqual(thread.participants, {"owner", "aster"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
