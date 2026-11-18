#!/usr/bin/env python3
"""App 前後文走 hybrid（owner 2026-11-08）：新的直接附（[[room]]），舊的讓她用 App 工具自己讀。

判據：
  ① `room_note` 只收她上次看過之後、不是她自己、不是觸發那則的發言；最多 8 則；帶會話 id 與工具提示；
  ② 群聊回合真的把這段帶進 `send(room_note=…)`；敲門補看時，更早沒看過的那幾則也在裡面（以前只接最新一則）；
  ③ `app.chat.read`：不給 id 就讀這一輪所在的會話；`before` 往前翻；App 拒絕時照實說、不編；
  ④ `app.post.read`：給 id 讀全文＋回覆、不給讀最新幾篇；主人顯示「オーナー」；
  ⑤ 只在 App 渠道、沒接 App Server 就不給；
  ⑥ `[[room]]` 出現在本輪段（歷史之後）。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import time
import types
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.app_group import GroupLedger
from agent.adapter import app_group_delay
from agent.adapter.app_group_knock import knock_tick
from agent.adapter.app_protocol import BoardError
from agent.adapter.app_read_tools import (AppChatReadTool, AppPostReadTool, ROOM_NOTE_MAX,
                                    build_app_read_tools, room_note)
from agent.adapter.app_server import AppChatAdapter
from agent.brain.messages import build_messages
from agent.brain.ports import BrainRequest

NOW = 1_800_000_000.0
ROOM = "cnv_room"
NAMES = {"nori": "ノリ", "mira": "ミラ", "aster": "アスター"}


def message(mid, author, text, at, kind="entity"):
    return {"id": mid, "conversation_id": ROOM, "created_at": at,
            "author": {"id": author, "kind": kind}, "parts": [{"kind": "text", "text": text}]}


class RoomNoteCase(unittest.TestCase):
    def test_only_fresh_lines_from_others_and_not_the_trigger(self):
        rows = [message("m0", "nori", "前に読んだ", NOW - 900),
                message("m1", "mira", "ねえ聞いて", NOW - 300),
                message("m2", "aster", "私の", NOW - 200),
                message("m3", "owner", "どうしたの", NOW - 100, kind="owner"),
                message("m4", "nori", "トリガー文", NOW - 10)]
        note = room_note(rows, conversation_id=ROOM, since=NOW - 600, skip_id="m4", me="aster",
                         names=NAMES)
        self.assertIn(f"conversation_id={ROOM}", note)
        self.assertIn("ミラ：ねえ聞いて", note)
        self.assertIn("オーナー：どうしたの", note)
        for gone in ("前に読んだ", "私の", "トリガー文"):
            self.assertNotIn(gone, note)
        self.assertIn("app_chat_read", note)

    def test_caps_and_empty(self):
        rows = [message(f"m{i}", "nori", f"行{i}", NOW - 100 + i) for i in range(20)]
        note = room_note(rows, conversation_id=ROOM, since=0, skip_id="", me="aster", names=NAMES)
        self.assertEqual(note.count("・"), ROOM_NOTE_MAX)
        self.assertIn("行19", note)
        self.assertNotIn("行11：", note)
        quiet = room_note([], conversation_id=ROOM, since=0, skip_id="", me="aster", names=NAMES)
        self.assertNotIn("・", quiet)
        self.assertIn("app_chat_read", quiet, "沒有新的也告訴她可以自己讀")


class Client:
    def __init__(self, rows=(), events=()):
        self.rows, self.event_rows = list(rows), list(events)
        self.sent, self.read = [], []

    def events(self, after, limit=100):
        rows = [row for row in self.event_rows if int(row["cursor"]) > int(after or 0)]
        return {"events": rows, "next_cursor": rows[-1]["cursor"] if rows else after, "has_more": False}

    def board_call(self, name, arguments):
        if name == "chat.messages":
            return {"messages": list(self.rows)[-int(arguments.get("limit") or 50):]}
        raise AssertionError(name)

    def mark_read(self, conversation_id, message_id):
        self.read.append((conversation_id, message_id))

    def send_message(self, conversation_id, parts, *, mutation_id, causation_id):
        self.sent.append(parts)


class Seen:
    def probability(self, entity, at, **_):
        return 1.0, "notify"

    def decision(self, entity, message, **_):
        from agent.adapter.app_group import AttentionDecision
        return AttentionDecision(True, 0.0, "notify")


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-room-note-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.turns = []

    def adapter(self, client, ledger):
        cursor = self.root / "aster.json"
        cursor.write_text(json.dumps({"cursor": "0"}), encoding="utf-8")

        def send(text, **kwargs):
            self.turns.append({"text": text, **kwargs})
            return {"frames": [{"kind": "done", "payload": {"text": "", "bubbles": []}}]}
        return AppChatAdapter("aster", client, send, cursor, lambda r: "", lambda r: [],
                              group_attention=Seen(), group_ledger=ledger,
                              names=NAMES, clock=lambda: NOW)

    def test_group_turn_carries_what_she_missed(self):
        rows = [message("m1", "mira", "ねえ聞いて", NOW - 300), message("m2", "nori", "うん", NOW - 5)]
        event = {"cursor": "1", "event_id": "e2", "type": "chat.message.created",
                 "payload": {"conversation_id": ROOM, "conversation_kind": "group",
                             "participants": ["owner", "aster", "nori", "mira"], "message": rows[1]}}
        ledger = GroupLedger(self.root / "ledger.json", clock=lambda: NOW)
        ledger.mark_seen(ROOM, "aster", NOW - 600)
        self.adapter(Client(rows, [event]), ledger).poll_once()
        note = self.turns[0]["room_note"]
        self.assertIn("ミラ：ねえ聞いて", note, "到達時沒看到的那則，這次補給她")
        self.assertNotIn("ノリ：うん", note, "觸發這一輪的那則本來就是本輪的話")

    def test_knock_catch_up_now_sees_more_than_the_latest(self):
        rows = [message("m1", "nori", "暇だね", NOW - 3600), message("m2", "mira", "それな", NOW - 1800)]
        ledger = GroupLedger(self.root / "ledger.json", clock=lambda: NOW)
        ledger.remember_room(ROOM, ["owner", "aster", "nori", "mira"])
        ledger.mark_seen(ROOM, "aster", NOW - 7200)
        adapter = self.adapter(Client(rows), ledger)
        self.assertEqual(knock_tick(adapter, NOW), "caught_up")
        app_group_delay.tick(adapter, NOW)        # 补看入队，到期才跑；「看到哪」也到那时才推进
        self.assertEqual(self.turns[0]["text"], "それな")
        self.assertIn("ノリ：暇だね", self.turns[0]["room_note"])


class FakeClient:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def board_call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.error:
            raise self.error
        return self.reply


class ToolCase(unittest.TestCase):
    def tool(self, cls, client):
        return cls("http://app", {"aster": "tok"}, NAMES, client_factory=lambda _url, _token: client)

    def ctx(self, source=f"app:{ROOM}"):
        return types.SimpleNamespace(entity="aster", source=source)

    def test_chat_read_defaults_to_this_room_and_pages(self):
        client = FakeClient({"messages": [message("m1", "owner", "おはよう", NOW, kind="owner")],
                             "next_before": "41"})
        result = self.tool(AppChatReadTool, client).run(self.ctx(), {"before": "57", "limit": 99})
        self.assertTrue(result.ok)
        self.assertEqual(client.calls[0], ("chat.messages",
                                           {"conversation_id": ROOM, "limit": 30, "before": "57"}))
        self.assertIn("オーナー：おはよう", result.data["text"])
        self.assertIn("before=41", result.data["text"])

    def test_chat_read_refusal_is_reported_not_invented(self):
        result = self.tool(AppChatReadTool, FakeClient(error=BoardError(404, "not_found"))).run(
            self.ctx(), {"conversation_id": "cnv_guest_dm"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_found")
        self.assertIn("推測で作らない", result.data["text"])
        missing = self.tool(AppChatReadTool, FakeClient()).run(self.ctx(source="phone:owner"), {})
        self.assertEqual(missing.error, "missing_conversation")

    def test_post_read_detail_and_feed(self):
        detail = {"post": {"id": "pst_1", "at": NOW, "author_kind": "entity", "author_id": "nori",
                           "body": "図鑑を見つけた", "likes": 2, "replies": 1},
                  "replies": [{"at": NOW, "author_kind": "owner", "author_id": "acct_owner",
                               "body": "いいね"}]}
        text = self.tool(AppPostReadTool, FakeClient(detail)).run(self.ctx(), {"id": "pst_1"}).data["text"]
        self.assertIn("[pst_1]", text)
        self.assertIn("ノリ：図鑑を見つけた", text)
        self.assertIn("└", text)
        self.assertIn("オーナー：いいね", text)
        feed_client = FakeClient({"posts": []})
        self.tool(AppPostReadTool, feed_client).run(self.ctx(), {})
        self.assertEqual(feed_client.calls[0][0], "feed")

    def test_only_on_app_channel_and_only_when_configured(self):
        self.assertEqual(build_app_read_tools({}), [])
        tools = build_app_read_tools({"BLUEAGENT_APP_SERVER_URL": "http://app",
                                      "BLUEAGENT_APP_SERVER_TOKENS": "aster=t"})
        self.assertEqual([tool.name for tool in tools], ["app.chat.read", "app.post.read"])
        self.assertTrue(all(tool.channels == ("app",) for tool in tools))

    def test_room_block_is_in_the_per_turn_section(self):
        request = BrainRequest(system="SYS", text="うん", principal="nori", entity="aster",
                               thread_id="th", mode="chat",
                               meta={"room_note": "この部屋。\n・07:00 ミラ：ねえ", "now": time.time(),
                                     "transcript": [{"role": "user", "speaker": "nori", "text": "前の話"}]})
        messages = build_messages(request)
        blob = [str(message.get("content") or "") for message in messages]
        room = next(index for index, text in enumerate(blob) if "[[room]]" in text)
        history = next(index for index, text in enumerate(blob) if "前の話" in text)
        self.assertGreater(room, history, "放在歷史之後，不動固定段的前綴快取")


if __name__ == "__main__":
    unittest.main(verbosity=2)
