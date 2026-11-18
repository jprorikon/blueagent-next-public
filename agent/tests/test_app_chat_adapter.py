#!/usr/bin/env python3
"""App Agent adapter：私聊、群聊环境模型（M5：各自拿起手机＋共享场账本）与游标重试。"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime

from demo_common import *  # noqa: F401,F403

from agent.adapter import app_group, app_group_delay
from agent.adapter.app_server import (
    AppChatAdapter, AttentionDecision, GroupAttention, GroupLedger, build_app_chat_adapters,
)


class FakeClient:
    def __init__(self, events):
        self.rows = list(events)
        self.sent = []
        self.read = []
        self.downloaded = []
        self.fail_send = False
        self.fail_media = False
        self.messages = []

    def board_call(self, name, arguments):
        if name == "chat.messages":
            return {"messages": list(self.messages)}
        raise AssertionError(name)

    def events(self, after, limit=100):
        selected = [row for row in self.rows if int(row["cursor"]) > int(after or 0)]
        return {"events": selected[:limit],
                "next_cursor": selected[min(limit, len(selected)) - 1]["cursor"] if selected else after,
                "has_more": len(selected) > limit}

    def mark_read(self, conversation_id, message_id):
        self.read.append((conversation_id, message_id))
        return {"conversation": {"id": conversation_id, "unread": 0}}

    def read_media(self, media_id):
        if self.fail_media:
            raise RuntimeError("media offline")
        self.downloaded.append(media_id)
        return {"id": media_id, "bytes": b"authorized-image", "mime": "image/png"}

    def send_message(self, conversation_id, parts, *, mutation_id, causation_id):
        if self.fail_send:
            raise RuntimeError("offline")
        self.sent.append({"conversation_id": conversation_id, "parts": parts,
                          "mutation_id": mutation_id, "causation_id": causation_id})
        return {"message": {"id": "reply"}}


class FakeAttention:
    """`seen` 固定；`@` 到的人一律看到（模拟 0.95 掷中），方便单测点名路径。"""

    def __init__(self, seen=True, delay=0.0):
        self.value = AttentionDecision(seen=seen, delay=delay)
        self.calls = []

    def decision(self, entity, message, **context):
        self.calls.append((entity, context))
        if context.get("call") == "at":
            return AttentionDecision(seen=True, delay=self.value.delay, kind="called")
        return self.value


def event(actor="owner", *, cursor="7", text="おはよう", event_id="evt_human",
          conversation_kind="direct", created_at=1762387200.0, parts=None):
    kind = "owner" if actor == "owner" else "entity"
    return {"cursor": cursor, "event_id": event_id, "type": "chat.message.created",
            "actor": {"id": actor, "kind": kind},
            "payload": {"conversation_id": "cnv_1", "conversation_kind": conversation_kind,
                        "participants": ["owner", "aster", "nori", "mira"],
                        "message": {"id": "msg_" + cursor, "created_at": created_at,
                                    "author": {"id": actor, "kind": kind},
                                    "parts": parts if parts is not None
                                    else [{"kind": "text", "text": text}]}}}


class AppChatAdapterCase(unittest.TestCase):
    #: ④ 点名优先用的叫名表（与生产 EntityRegistry 一致：id／显示名／nicknames）
    ALIASES = {"aster": ("aster", "アスター", "アスター"), "nori": ("nori", "ノリ", "ノリ"),
               "mira": ("mira", "ミラ", "ミラ")}

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="ba-app-adapter-")
        self.addCleanup(self.folder.cleanup)
        self.root = pathlib.Path(self.folder.name)
        self.cursor = self.root / "cursor.json"
        self.cursor.write_text(json.dumps({"cursor": "0"}), encoding="utf-8")
        self.calls = []

    def result(self, text, bubbles):
        return {"ok": True, "frames": [{"kind": "done", "payload": {
            "text": text, "bubbles": bubbles}}]}

    def adapter(self, client, entity="aster", *, cursor_path=None, attention=None,
                ledger=None, reply=("一。二。", ["一。", "二。"]),
                aliases=None, max_attempts=5, min_retry_s=600.0, clock=None):
        def send(text, **kwargs):
            self.calls.append({"text": text, "from": entity, **kwargs})
            if reply is None:                    # 判重复：这一轮没有任何帧
                return {"ok": True, "duplicate": True, "frames": []}
            return self.result(*reply)
        path = pathlib.Path(cursor_path) if cursor_path is not None else self.cursor
        if entity != "aster" and cursor_path is None:
            path = self.root / f"{entity}-cursor.json"
            path.write_text(json.dumps({"cursor": "0"}), encoding="utf-8")
        return AppChatAdapter(
            entity, client, send, path,
            lambda result: str(result["frames"][-1]["payload"]["text"]),
            lambda result: list(result["frames"][-1]["payload"]["bubbles"]),
            group_attention=attention, group_ledger=ledger,
            aliases=self.ALIASES if aliases is None else aliases,
            max_attempts=max_attempts, min_retry_s=min_retry_s,
            **({"clock": clock} if clock is not None else {}))

    def group_adapter(self, client, entity, ledger, *, seen=True, reply=None, cursor_path=None):
        return self.adapter(client, entity, cursor_path=cursor_path,
                            attention=FakeAttention(seen=seen), ledger=ledger,
                            reply=reply or ("一。二。", ["一。", "二。"]))

    def fresh_cursor(self, name: str) -> pathlib.Path:
        """在 0 位置开一条新线（不写档的话构造时会当首次上线、只做 prime）。"""
        path = self.root / name
        path.write_text(json.dumps({"cursor": "0"}), encoding="utf-8")
        return path

    def test_human_event_runs_turn_and_commits_parts_with_causation(self):
        client = FakeClient([event()])
        adapter = self.adapter(client)
        self.assertEqual(adapter.poll_once(), 1)
        self.assertEqual(self.calls[0]["entity"], "aster")
        self.assertEqual(self.calls[0]["principal"], "owner")
        self.assertEqual(self.calls[0]["source_override"], "app:cnv_1")
        self.assertEqual(self.calls[0]["request_id"], "msg_7")
        self.assertNotIn("group", self.calls[0], "私聊不能套用群聊注意力路由")
        self.assertEqual(client.read, [], "私聊行为保持原样")
        self.assertEqual(client.sent, [{"conversation_id": "cnv_1", "parts": ["一。", "二。"],
            "mutation_id": "agent:msg_7", "causation_id": "evt_human"}])
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "7")
        self.assertEqual(adapter.poll_once(), 0)
        self.assertEqual(len(self.calls), 1, "重放游标不能重复起回合")

    def test_owner_turn_is_work_and_peer_turn_is_chat(self):
        """2026-11-11：App 以前寫死 chat ⇒ 主人在 App 被標成 SOUL.md 說的「只有客人才走的 chat」。
        現在與 HTTP 閘／Discord 同：主人＝work，其他角色（與客人）＝chat。"""
        self.adapter(FakeClient([event()])).poll_once()
        self.assertEqual(self.calls[-1]["mode"], "work")
        self.calls.clear()
        self.adapter(FakeClient([event("nori", cursor="8", event_id="evt_peer")]),
                     cursor_path=self.fresh_cursor("cursor-peer.json")).poll_once()
        if self.calls:                      # 角色私聊可能因預算不接話；有接的話一定是 chat
            self.assertEqual(self.calls[-1]["mode"], "chat")

    def test_photo_only_direct_message_enters_image_turn_with_authorized_bytes(self):
        parts = [{"kind": "photo", "media_id": "m0123456789abcdef"}]
        client = FakeClient([event(cursor="71", text="", parts=parts)])
        self.adapter(client).poll_once()
        self.assertEqual(client.downloaded, ["m0123456789abcdef"])
        self.assertEqual(self.calls[0]["text"], "（画像）")
        self.assertEqual(self.calls[0]["kind"], "image")
        self.assertEqual(self.calls[0]["media_list"], [{
            "id": "m0123456789abcdef", "bytes": b"authorized-image", "mime": "image/png"}])
        self.assertEqual(self.calls[0]["asked_parts"], parts)
        self.assertNotIn("path", repr(self.calls[0]["media_list"]))
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "71")

    def test_group_photo_downloads_only_after_seen(self):
        parts = [{"kind": "photo", "media_id": "m0123456789abcdef"}]
        row = event(cursor="72", text="", parts=parts, conversation_kind="group")
        unseen = FakeClient([row])
        ledger = GroupLedger(self.root / "photo-ledger.json")
        self.group_adapter(unseen, "aster", ledger, seen=False).poll_once()
        self.assertEqual((unseen.downloaded, unseen.read, self.calls), ([], [], []))
        seen = FakeClient([row])
        self.group_adapter(seen, "nori", ledger, seen=True).poll_once()
        self.assertEqual(seen.downloaded, ["m0123456789abcdef"])
        self.assertEqual(seen.read, [("cnv_1", "msg_72")])
        self.assertEqual(self.calls[-1]["kind"], "image")

    def test_media_failure_keeps_the_turn_queued_and_does_not_mark_group_read(self):
        """群聊回合一律入队（2026-11-16）：游标照推进，失败的那一轮留在队列里重试。"""
        from agent.adapter import app_group_delay
        parts = [{"kind": "photo", "media_id": "m0123456789abcdef"}]
        client = FakeClient([event(cursor="73", text="", parts=parts,
                                   conversation_kind="group")])
        client.fail_media = True
        adapter = self.group_adapter(client, "aster",
                                     GroupLedger(self.root / "retry-photo.json"))
        with self.assertRaises(RuntimeError):
            adapter.poll_once()
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "73", "入队之后游标就推进")
        self.assertEqual(app_group_delay.pending_rooms(adapter), {"cnv_1"}, "那一轮还在队列里")
        self.assertEqual((client.read, self.calls), ([], []))
        client.fail_media = False
        adapter.poll_once()
        self.assertEqual(client.read, [("cnv_1", "msg_73")])
        self.assertEqual(self.calls[-1]["kind"], "image")
        self.assertEqual(app_group_delay.pending_rooms(adapter), set())

    def test_own_event_never_triggers_another_turn(self):
        client = FakeClient([event("aster", cursor="8", event_id="evt_own")])
        adapter = self.adapter(client)
        self.assertEqual(adapter.poll_once(), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(client.sent, [])
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "8")

    def test_failed_mcp_send_keeps_cursor_for_idempotent_retry(self):
        client = FakeClient([event(cursor="9")])
        client.fail_send = True
        adapter = self.adapter(client)
        with self.assertRaises(RuntimeError):
            adapter.poll_once()
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "0")
        client.fail_send = False
        adapter.poll_once()
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "9")
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.sent[0]["mutation_id"], "agent:msg_9")

    def test_first_start_primes_without_replaying_history(self):
        self.cursor.unlink()
        client = FakeClient([event(cursor="10")])
        adapter = self.adapter(client)
        self.assertEqual(adapter.poll_once(), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "10")

    def test_group_unseen_has_no_turn_memory_or_read(self):
        ledger = GroupLedger(self.root / "ledger.json")
        client = FakeClient([event(cursor="11", conversation_kind="group")])
        adapter = self.group_adapter(client, "aster", ledger, seen=False)
        self.assertEqual(adapter.poll_once(), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(client.read, [])
        self.assertEqual(client.sent, [])
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "11")

    def test_group_reply_is_limited_to_three_bubbles_and_has_stable_delay(self):
        now = [1000.0]
        ledger = GroupLedger(self.root / "ledger.json", clock=lambda: now[0])
        client = FakeClient([event(cursor="15", conversation_kind="group", created_at=now[0])])
        adapter = self.adapter(
            client, attention=FakeAttention(delay=1.25), ledger=ledger, clock=lambda: now[0],
            reply=("四。", ["一。", "二。", "三。", "四。"]))
        adapter.poll_once()
        self.assertEqual(client.sent, [], "到期前只入队，不跑回合")
        now[0] += 1.25
        adapter.poll_once()
        self.assertEqual(client.sent[0]["parts"], ["一。", "二。", "三。"])

    def test_location_events_only_publish_private_perception_and_advance_cursor(self):
        received = []
        rows = [
            {"cursor": "81", "event_id": "loc_1", "type": "location.updated",
             "payload": {"location": {"principal": "owner", "lat": 35.68, "lon": 139.77}}},
            {"cursor": "82", "event_id": "loc_2", "type": "location.revoked",
             "payload": {"account_id": "owner", "purpose": "weather-v1"}},
        ]
        client = FakeClient(rows)
        adapter = self.adapter(client)
        adapter.on_location = lambda *args: received.append(args)
        self.assertEqual(adapter.poll_once(), 2)
        self.assertEqual((self.calls, client.sent, client.read), ([], [], []))
        self.assertEqual([row[:2] for row in received],
                         [("aster", "location.updated"), ("aster", "location.revoked")])
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "82")

    def test_composition_root_shares_ledger_and_emits_coordinate_free_location_events(self):
        published = []
        world_events = types.SimpleNamespace(
            publish=lambda kind, text, **kwargs: published.append((kind, text, kwargs)))
        state = types.SimpleNamespace(
            app=types.SimpleNamespace(data_dir=self.root, world_events=world_events),
            send=lambda *_a, **_k: {}, reply_text=lambda _r: "",
            reply_bubbles=lambda _r: [], _log_command=lambda _m: None)
        adapters = build_app_chat_adapters(state, {
            "BLUEAGENT_APP_SERVER_URL": "http://127.0.0.1:8788",
            "BLUEAGENT_APP_SERVER_TOKENS": "aster=one,nori=two"})
        self.assertEqual(len(adapters), 2)
        self.assertIs(adapters[0].group_ledger, adapters[1].group_ledger, "三人共用一本场账本")
        self.assertEqual(adapters[0].group_ledger.path,
                         self.root / "app-adapter" / "group-ledger.json")
        adapters[0].on_location("aster", "location.updated", {
            "location": {"principal": "owner", "lat": 35.68, "lon": 139.77}})
        adapters[0].on_location("aster", "location.revoked", {"account_id": "owner"})
        self.assertEqual(published, [
            ("location", "主人のいる場所が変わった", {"entities": ("aster",)}),
            ("location", "位置共有が停止された", {"entities": ("aster",)}),
        ])
        self.assertNotIn("35.68", repr(published))
        self.assertNotIn("139.77", repr(published))
        self.assertTrue(all(not kwargs.get("public", False)
                            for _kind, _text, kwargs in published))


    # ── 群聊自聊 M5a／M5b（docs/GROUP-SELF-CHAT-20261108.md）──────────────────────

    def three(self, row, ledger, *, seen=True, replies=None):
        """同一条消息依序给 aster／nori／mira 各自的 adapter。"""
        clients = {}
        for entity in ("aster", "nori", "mira"):
            clients[entity] = FakeClient([dict(row)])
            reply = (replies or {}).get(entity)
            self.adapter(clients[entity], entity,
                         cursor_path=self.fresh_cursor(f"{entity}-{row['cursor']}.json"),
                         attention=FakeAttention(seen=seen), ledger=ledger,
                         reply=reply or ("一。二。", ["一。", "二。"])).poll_once()
        return clients

    def test_owner_message_gets_at_most_two_responders_and_the_rest_watch(self):
        ledger = GroupLedger(self.root / "m5-two.json")
        clients = self.three(event(cursor="12", conversation_kind="group"), ledger)
        self.assertEqual([len(clients[e].sent) for e in ("aster", "nori", "mira")], [1, 1, 0])
        self.assertEqual([call["addressed_to"] for call in self.calls], ["", "", "ignore"])
        self.assertEqual(self.calls[0]["participants"], ["aster", "mira", "nori", "owner"])
        for client in clients.values():
            self.assertEqual(client.read, [("cnv_1", "msg_12")], "看到才可以已读")

    def test_silent_responder_still_uses_up_its_slot(self):
        """拿到名额却沉默＝合法；名额不还，第三个人不能补位。"""
        ledger = GroupLedger(self.root / "m5-silent.json")
        clients = self.three(event(cursor="13", conversation_kind="group"), ledger,
                             replies={"aster": ("", [])})
        self.assertEqual([len(clients[e].sent) for e in ("aster", "nori", "mira")], [0, 1, 0])
        self.assertEqual(self.calls[2]["addressed_to"], "ignore")

    def test_adapter_cannot_sleep_at_all(self):
        """群聊延遲只靠到期隊列；適配器連 `sleep` 參數都沒有了（2026-11-16），拉取線程不可能再被睡住。"""
        import inspect
        from agent.adapter import app_group_delay
        self.assertNotIn("sleep", inspect.signature(AppChatAdapter).parameters)
        self.assertNotIn("sleep(", inspect.getsource(app_group_delay))

    def test_slot_is_taken_before_her_delay(self):
        """延迟＝她多久才拿起手机；这段等待里名额已经归她，敲门围观抢不走（2026-11-15 实测回归）。"""
        now = [2000.0]
        ledger = GroupLedger(self.root / "m5-delay.json", clock=lambda: now[0])
        client = FakeClient([event(cursor="21", conversation_kind="group", created_at=now[0])])
        adapter = self.adapter(
            client, "aster", ledger=ledger, clock=lambda: now[0],
            attention=FakeAttention(seen=True, delay=121.0))
        adapter.poll_once()
        reserved = list((ledger._data.get("reservations") or {}).get("cnv_1\0msg_21") or [])
        self.assertEqual(reserved, ["aster"], "延迟之前就要占名额")
        self.assertEqual(client.sent, [])
        now[0] += 121.0
        adapter.poll_once()
        self.assertEqual(len(client.sent), 1)

    def test_character_message_can_start_a_turn(self):
        """以前 Agent 的发言只进旁观；现在她们之间可以自己接一句（M5a 的验收）。"""
        ledger = GroupLedger(self.root / "m5-ai.json")
        client = FakeClient([event("nori", cursor="14", conversation_kind="group")])
        self.group_adapter(client, "aster", ledger).poll_once()
        self.assertEqual(self.calls[0]["addressed_to"], "")
        self.assertEqual(len(client.sent), 1)
        room = ledger._data["rooms"]["cnv_1"]
        self.assertEqual(room["ai_chain"], 1, "回 AI 的一句算进互接链")

    def test_mentioned_character_keeps_her_slot(self):
        """@ミラ：ミラ保留一个名额，另一个名额先到先得，第三人旁观。"""
        ledger = GroupLedger(self.root / "m5-call.json")
        clients = self.three(event(cursor="18", conversation_kind="group",
                                   text="@ミラ その写真、あとで見せて"), ledger)
        self.assertEqual([len(clients[e].sent) for e in ("aster", "nori", "mira")], [1, 0, 1])

    def test_character_at_mention_is_a_call(self):
        """M5：角色之间的 `@` 也算叫人（拿起率 0.95），不再只有主人能点名。"""
        attention = FakeAttention(seen=False)
        client = FakeClient([event("nori", cursor="26", conversation_kind="group",
                                   text="@アスター それ見た？")])
        self.adapter(client, "aster", attention=attention,
                     ledger=GroupLedger(self.root / "m5-at.json")).poll_once()
        self.assertEqual(attention.calls[0][1]["call"], "at")
        self.assertEqual(len(client.sent), 1)

    def test_failed_group_send_keeps_the_same_slot_on_retry(self):
        ledger = GroupLedger(self.root / "m5-retry.json")
        row = event(cursor="16", conversation_kind="group")
        first = FakeClient([row])
        first.fail_send = True
        adapter = self.group_adapter(first, "aster", ledger)
        with self.assertRaises(RuntimeError):
            adapter.poll_once()
        first.fail_send = False
        adapter.poll_once()
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(ledger._data["rooms"]["cnv_1"]["turn_no"], 1, "重试不算第二个 turn")

    def test_bubble_quota_is_rechecked_right_before_sending(self):
        ledger = GroupLedger(self.root / "m5-quota.json")
        ledger._data.setdefault("bubbles", {})["aster"] = [time.time()] * app_group.BUBBLES_PER_HOUR
        client = FakeClient([event(cursor="19", conversation_kind="group")])
        self.group_adapter(client, "aster", ledger).poll_once()
        self.assertEqual(client.sent, [], "一小时 40 泡泡打满就不再发")

    def test_decision_is_remembered_across_retries_and_restarts(self):
        path = self.root / "m5-memo.json"
        ledger = GroupLedger(path)
        calls = []

        def compute():
            calls.append(1)
            return AttentionDecision(True, 3.0, "notify")
        first = ledger.decision("cnv", "msg_1", "aster", compute)
        again = GroupLedger(path).decision("cnv", "msg_1", "aster", compute)
        self.assertEqual((first, again, len(calls)), (again, first, 1))

    def test_ledger_rules_gap_chain_and_session(self):
        ledger = GroupLedger(self.root / "m5-rules.json")
        t0 = 1_800_000_000.0
        ok = lambda msg, who, at, **kw: ledger.reserve("cnv", msg, who, at=at, **{
            "from_ai": True, "owner": False, **kw})
        self.assertEqual(ok("m0", "aster", t0), (True, "ok"))
        self.assertEqual(ok("m1", "aster", t0 + 10), (False, "same_speaker_gap"), "同人 20 秒")
        # AI 互接 6 turn：aster 已经 1 次，再接 5 次就满
        for index, who in enumerate(("nori", "mira", "aster", "nori", "mira"), start=2):
            self.assertTrue(ok(f"m{index}", who, t0 + 21 * index)[0])
        self.assertEqual(ok("m9", "aster", t0 + 21 * 9), (False, "ai_chain"))
        ledger.note_message("cnv", owner=True, at=t0 + 200)          # owner 插话 ⇒ 链归零
        self.assertTrue(ok("m10", "nori", t0 + 210, from_ai=False, owner=True)[0])
        self.assertTrue(ok("m11", "mira", t0 + 240)[0])          # 第 8 turn
        self.assertEqual(ok("m12", "aster", t0 + 270), (False, "session_full"), "一场 8 turn")
        self.assertTrue(ok("m13", "aster", t0 + 300)[0], "300 秒后新开一场")

    def test_release_restores_previous_spoke_timestamp(self):
        ledger = GroupLedger(self.root / "release-spoke.json")
        t0 = 1_800_000_000.0
        self.assertTrue(ledger.reserve("cnv", "old", "aster", at=t0,
                                       from_ai=False, owner=False)[0])
        t1 = t0 + app_group.SAME_SPEAKER_GAP_S + 5
        self.assertTrue(ledger.reserve("cnv", "delayed", "aster", at=t1,
                                       from_ai=False, owner=False)[0])
        state = ledger.reservation_state("cnv", "delayed", "aster")
        self.assertTrue(ledger.release("cnv", "delayed", "aster", at=t1, state=state))
        room = ledger._data["rooms"]["cnv"]
        self.assertEqual(room["spoke"]["aster"], t0)
        self.assertEqual(ledger.reserve("cnv", "too-soon", "aster", at=t0 + 5,
                                        from_ai=False, owner=False),
                         (False, "same_speaker_gap"))

    def test_release_undoes_session_flip_and_chain_increment(self):
        ledger = GroupLedger(self.root / "release-session.json")
        t0 = 1_800_000_000.0
        room = ledger._room("cnv")
        room.update(opened_at=t0, turn_no=app_group.SESSION_TURNS,
                    ai_chain=3, last_turn_at=t0)
        at = t0 + app_group.SESSION_S + 1
        self.assertTrue(ledger.reserve("cnv", "new-session", "aster", at=at,
                                       from_ai=True, owner=True)[0])
        self.assertEqual((room["opened_at"], room["turn_no"], room["ai_chain"]), (at, 1, 4))
        state = ledger.reservation_state("cnv", "new-session", "aster")
        ledger.release("cnv", "new-session", "aster", at=at, from_ai=True, state=state)
        self.assertEqual((room["opened_at"], room["turn_no"], room["ai_chain"]),
                         (t0, app_group.SESSION_TURNS, 3))

    def test_queue_overflow_releases_evicted_reservation_and_logs(self):
        now = [3_000.0]
        ledger = GroupLedger(self.root / "queue-overflow.json", clock=lambda: now[0])
        adapter = self.adapter(FakeClient([]), "aster", ledger=ledger, clock=lambda: now[0])
        logs = []
        adapter.log = logs.append
        for index in range(app_group_delay.MAX_ITEMS + 1):
            at = 10_000.0 + index * 30
            message_id = f"m{index}"
            self.assertTrue(ledger.reserve("cnv_1", message_id, "aster", at=at,
                                           from_ai=False, owner=True)[0])
            app_group_delay.hold(adapter, {}, conversation_id="cnv_1", message_id=message_id,
                                 at=at, delay=60.0, granted=True, reason="ok", from_ai=False)
        self.assertEqual(len(app_group_delay._load(adapter)), app_group_delay.MAX_ITEMS)
        self.assertNotIn("aster", (ledger._data.get("reservations") or {}).get("cnv_1\0m0", []))
        self.assertTrue(ledger.reserve("cnv_1", "m0", "nori", at=10_000.0,
                                       from_ai=False, owner=True)[0])
        self.assertTrue(any("queue_overflow" in line for line in logs))

    def test_attention_formula_and_modifier_order(self):
        clock = types.SimpleNamespace(state=lambda _at: self.state)
        attention = GroupAttention(circadian={"aster": clock})
        noon = datetime(2026, 11, 9, 12, 0).timestamp()
        three_am = datetime(2026, 11, 9, 3, 0).timestamp()
        self.state = "awake"
        self.assertEqual(attention.probability("aster", noon, call="at"), (0.95, "called"))
        self.assertEqual(attention.probability("aster", noon, call="named"), (0.70, "named"))
        near, _ = attention.probability("aster", noon, last_seen_at=noon - 300)
        far, _ = attention.probability("aster", noon, last_seen_at=noon - 3 * 3600)
        self.assertLess(near, far, "距上次看到越久越可能拿起")
        self.assertEqual(attention.probability("aster", three_am)[0], 0.0, "2–6 点没人看手机")
        self.assertEqual(attention.probability("aster", three_am, owner_recent=True)[0], 0.5,
                         "owner 30 分内说过话 ⇒ 地板 0.5")
        self.state = "asleep"
        self.assertEqual(attention.probability("aster", noon, owner_recent=True, call="named"),
                         (0.0, "asleep"), "睡着时叫名字、owner 地板都叫不醒")
        self.assertEqual(attention.probability("aster", noon, call="at")[0], 0.3, "@ 按生物钟打折")
        self.state = "winding"
        self.assertEqual(attention.probability("aster", noon, call="at")[0], 0.8)
        busy = {round(GroupAttention.busy_factor(who, noon), 6) for who in ("aster", "nori", "mira")}
        self.assertTrue(all(0.3 <= value <= 2.5 for value in busy) and len(busy) > 1)
        for kind, median in app_group.DELAY_MEDIAN.items():
            value = GroupAttention.delay("aster", "msg_x", kind)
            self.assertTrue(0.3 * median <= value <= 3.0 * median, kind)
        decision = attention.decision("aster", {"id": "m1", "created_at": noon}, call="at")
        self.assertEqual(decision, attention.decision("aster", {"id": "m1", "created_at": noon},
                                                      call="at"), "同一条消息的掷骰稳定")

    # ── REVIEW 2026-11-07 的 9 条里与这个 adapter 有关的四条 ────────────────────

    def test_group_round_uses_its_own_scope_and_scene(self):
        """① App 群聊不能和 Discord 大厅共用一条 thread。"""
        ledger = GroupLedger(self.root / "ledger.json")
        client = FakeClient([event(cursor="17", conversation_kind="group")])
        self.group_adapter(client, "aster", ledger).poll_once()
        self.assertEqual(self.calls[0]["scope_override"], "group:cnv_1")
        self.assertEqual(self.calls[0]["scene_override"], "app-group")
        self.assertNotIn("discord", repr(self.calls[0].get("scope_override")))

    def test_unsent_reply_is_resent_without_running_the_turn_again(self):
        """② 提交失败后重试只补发，不重跑 LLM（重跑会被判重复、回复永远丢）。"""
        client = FakeClient([event(cursor="19")])
        client.fail_send = True
        adapter = self.adapter(client)
        with self.assertRaises(RuntimeError):
            adapter.poll_once()
        self.assertEqual(len(self.calls), 1)
        client.fail_send = False
        adapter.poll_once()
        self.assertEqual(len(self.calls), 1, "补发不能重新起一轮")
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.sent[0]["parts"], ["一。", "二。"])
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "19")

    def test_duplicate_turn_with_pending_reply_still_delivers(self):
        """② 同一进程内判重复（没有帧）时，用落盘的那份回复补交。"""
        client = FakeClient([event(cursor="20")])
        client.fail_send = True
        path = self.fresh_cursor("dup.json")
        first = self.adapter(client, cursor_path=path)
        with self.assertRaises(RuntimeError):
            first.poll_once()
        client.fail_send = False
        second = self.adapter(client, reply=None,          # 重跑只会得到 duplicate
                              cursor_path=path)
        self.assertEqual(second.poll_once(), 1)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.sent[0]["mutation_id"], "agent:msg_20")
        self.assertEqual(len(self.calls), 1, "补发不应该再跑一轮")

    def test_missing_photo_degrades_to_text_turn(self):
        """③ 图片被删（404）不该卡死整条线：退化成纯文字回合。"""
        parts = [{"kind": "photo", "media_id": "mdeadbeefdeadbeef"}]

        class Gone(FakeClient):
            def read_media(self, media_id):
                raise RuntimeError("app_server_http_404")

        client = Gone([event(cursor="21", text="これ見て",
                             parts=[{"kind": "text", "text": "これ見て"}] + parts)])
        adapter = self.adapter(client)
        self.assertEqual(adapter.poll_once(), 1)
        self.assertEqual(self.calls[0]["text"], "これ見て")
        self.assertNotIn("kind", self.calls[0])
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "21")

    def test_event_is_dead_lettered_after_attempts_and_retry_window(self):
        """③ 网络类错误要**次数到上限而且重试够久**才放弃（REVIEW 2026-11-08：以前≈10 秒就放弃）。"""
        client = FakeClient([event(cursor="22")])
        client.fail_media = True                        # 回合之前就失败：没有待补发的回复
        now = [1000.0]
        adapter = self.adapter(client, cursor_path=self.fresh_cursor("dead.json"),
                               max_attempts=2, min_retry_s=300.0, clock=lambda: now[0])
        event_with_photo = client.rows[0]
        event_with_photo["payload"]["message"]["parts"].append(
            {"kind": "photo", "media_id": "mphoto0000000001"})
        for _ in range(3):                               # 次数到了但时间不够 ⇒ 还在重试
            with self.assertRaises(RuntimeError):
                adapter.poll_once()
            now[0] += 10.0
        self.assertEqual(json.loads((self.root / "dead.json").read_text())["cursor"], "0")
        now[0] += 300.0                                  # 时间也到了 ⇒ 登记并跳过
        self.assertEqual(adapter.poll_once(), 1)
        self.assertEqual(json.loads((self.root / "dead.json").read_text())["cursor"], "22")
        dead = json.loads((self.root / "dead-dead-letter.json").read_text())
        self.assertEqual([row["event_id"] for row in dead["dead"]], ["evt_human"])
        self.assertEqual(dead["failures"], {})

    def test_unsent_reply_is_never_dead_lettered_on_transient_errors(self):
        """REVIEW 2026-11-08：App Server 重启时送不出的回覆要一直补发，不能被当成坏事件丢掉。"""
        client = FakeClient([event(cursor="23")])
        client.fail_send = True
        now = [1000.0]
        adapter = self.adapter(client, cursor_path=self.fresh_cursor("keep.json"),
                               max_attempts=2, min_retry_s=60.0, clock=lambda: now[0])
        for _ in range(5):
            with self.assertRaises(RuntimeError):
                adapter.poll_once()
            now[0] += 120.0                              # 次数与时间都远超上限
        self.assertEqual(json.loads((self.root / "keep.json").read_text())["cursor"], "0")
        client.fail_send = False                         # 服务恢复 ⇒ 补发那一份
        self.assertEqual(adapter.poll_once(), 1)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(len(self.calls), 1, "补发不重跑回合")

    def test_named_not_found_error_is_fatal(self):
        """`_request` 带 JSON 错误体时码是错误名（`app_server_http_not_found`），也要立刻放弃。"""
        client = FakeClient([event(cursor="24")])

        def gone(conversation_id, parts, **_kwargs):
            raise RuntimeError("app_server_http_not_found")
        client.send_message = gone
        adapter = self.adapter(client, cursor_path=self.fresh_cursor("gone.json"))
        self.assertEqual(adapter.poll_once(), 1)
        self.assertEqual(json.loads((self.root / "gone.json").read_text())["cursor"], "24")
        self.assertFalse((self.root / "gone-unsent.json").exists()
                         and json.loads((self.root / "gone-unsent.json").read_text())["replies"],
                         "放弃的事件不能留下永远补发不了的回复")

    # ── WP8：群聊延迟与事件拉取解耦（架构审核 E1）────────────────────────────────

    def test_direct_chat_during_group_delay_runs_before_delay_expires(self):
        """WP8 ①②：群聊延迟等待期间到达的私聊在延迟到期前先处理；到期后群聊照常执行，内容与顺序不变。"""
        now = [1000.0]
        ledger = GroupLedger(self.root / "wp8-dm.json", clock=lambda: now[0])
        group_evt = event(cursor="31", text="群里聊一句", event_id="evt_grp_31",
                          conversation_kind="group", created_at=1000.0)
        dm_evt = event(cursor="32", text="私聊找你", event_id="evt_dm_32",
                       conversation_kind="direct", created_at=1005.0)
        client = FakeClient([group_evt, dm_evt])
        adapter = self.adapter(
            client, "aster", ledger=ledger,
            attention=FakeAttention(seen=True, delay=30.0),
            reply=("四。", ["一。", "二。", "三。", "四。"]),
            clock=lambda: now[0],
        )
        # t=1000：同一页里先收到群聊（delay=30s 入队），紧接着的私聊立刻在同轮处理完，不阻塞在群聊后面
        self.assertEqual(adapter.poll_once(), 2)
        self.assertEqual(json.loads(self.cursor.read_text())["cursor"], "32")
        self.assertEqual([call["request_id"] for call in self.calls], ["msg_32"],
                         "私聊必须在群聊延迟到期之前先被处理")
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.sent[0]["mutation_id"], "agent:msg_32")

        # t=1035：延迟到期，群聊回合照常执行，发言内容与泡泡裁剪不变
        now[0] = 1035.0
        self.assertEqual(adapter.poll_once(), 1)
        self.assertEqual([call["request_id"] for call in self.calls], ["msg_32", "msg_31"])
        self.assertEqual(len(client.sent), 2)
        self.assertEqual(client.sent[1]["mutation_id"], "agent:msg_31")
        self.assertEqual(client.sent[1]["parts"], ["一。", "二。", "三。"])

    def test_delayed_group_turn_persists_across_restart_and_runs_exactly_once(self):
        """WP8 ③：延迟期间进程重启 ⇒ 重启后该回合仍从磁盘恢复并执行一次，且只执行一次。"""
        now = [2000.0]
        ledger_path = self.root / "wp8-restart-ledger.json"
        cursor_path = self.fresh_cursor("wp8-restart-cursor.json")
        grp_evt = event(cursor="33", text="重启前那句", event_id="evt_grp_33",
                        conversation_kind="group", created_at=2000.0)
        client = FakeClient([grp_evt])

        first = self.adapter(
            client, "aster", cursor_path=cursor_path,
            ledger=GroupLedger(ledger_path, clock=lambda: now[0]),
            attention=FakeAttention(seen=True, delay=30.0),
            clock=lambda: now[0],
        )
        self.assertEqual(first.poll_once(), 1)
        self.assertEqual(self.calls, [], "还在延迟期内，尚未跑回合")
        self.assertEqual(client.sent, [])
        delay_file = first.sidecar.path_of("group-delay")
        self.assertTrue(delay_file.is_file(), "延迟队列必须已落盘")

        # t=2015 模拟进程重启：用同一 cursor_path 与 ledger_path 新建实例，尚未到期
        now[0] = 2015.0
        second = self.adapter(
            client, "aster", cursor_path=cursor_path,
            ledger=GroupLedger(ledger_path, clock=lambda: now[0]),
            attention=FakeAttention(seen=True, delay=30.0),
            clock=lambda: now[0],
        )
        self.assertEqual(second.poll_once(), 0)
        self.assertEqual(client.sent, [])

        # t=2035 到期：恢复出的回合执行一次
        now[0] = 2035.0
        self.assertEqual(second.poll_once(), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.sent[0]["mutation_id"], "agent:msg_33")

        # 继续轮询或再次重启：绝不重复执行第二次
        now[0] = 2050.0
        self.assertEqual(second.poll_once(), 0)
        third = self.adapter(
            client, "aster", cursor_path=cursor_path,
            ledger=GroupLedger(ledger_path, clock=lambda: now[0]),
            attention=FakeAttention(seen=True, delay=30.0),
            clock=lambda: now[0],
        )
        self.assertEqual(third.poll_once(), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(client.sent), 1)

    def test_expiration_recheck_aborts_and_releases_slot_for_other_character(self):
        """只有她自己已对更新的消息占位／发言才放弃；放弃后释放名额，别的角色可以拿到。"""
        now = [3000.0]
        ledger = GroupLedger(self.root / "wp8-recheck.json", clock=lambda: now[0])
        msg = event(cursor="41", text="大家在吗", event_id="evt_grp_41",
                    conversation_kind="group", created_at=3000.0)
        aster_client = FakeClient([dict(msg)])
        aster = self.adapter(
            aster_client, "aster", cursor_path=self.fresh_cursor("aster-41.json"),
            ledger=ledger, attention=FakeAttention(seen=True, delay=30.0),
            clock=lambda: now[0])
        aster.poll_once()
        nori_client = FakeClient([dict(msg)])
        self.adapter(
            nori_client, "nori", cursor_path=self.fresh_cursor("nori-41.json"),
            ledger=ledger, attention=FakeAttention(seen=True, delay=0.0),
            clock=lambda: now[0]).poll_once()
        self.assertEqual(
            ledger.reserve("cnv_1", "msg_41", "mira", at=3000.0,
                           from_ai=False, owner=True),
            (False, "message_full"))

        self.assertTrue(ledger.reserve("cnv_1", "msg_newer", "aster", at=3025.0,
                                       from_ai=False, owner=True)[0])
        now[0] = 3035.0
        aster.poll_once()
        self.assertEqual(aster_client.sent, [], "她自己已往后走，旧回合才不发言")
        mira_client = FakeClient([dict(msg)])
        self.adapter(
            mira_client, "mira", cursor_path=self.fresh_cursor("mira-41.json"),
            ledger=ledger, attention=FakeAttention(seen=True, delay=0.0),
            clock=lambda: now[0]).poll_once()
        self.assertEqual(len(mira_client.sent), 1, "释放后的名额应可被其他角色拿到")

    def test_other_character_reply_does_not_cancel_and_is_in_room_note(self):
        now = [5000.0]
        ledger = GroupLedger(self.root / "other-reply.json", clock=lambda: now[0])
        msg = event(cursor="51", text="主人问大家", event_id="evt_grp_51",
                    conversation_kind="group", created_at=now[0])
        owner_message = dict(msg["payload"]["message"])
        aster_client = FakeClient([dict(msg)])
        aster_client.messages = [owner_message]
        aster = self.adapter(
            aster_client, "aster", cursor_path=self.fresh_cursor("aster-51.json"), ledger=ledger,
            attention=FakeAttention(seen=True, delay=30.0), clock=lambda: now[0])
        nori_client = FakeClient([dict(msg)])
        nori = self.adapter(
            nori_client, "nori", cursor_path=self.fresh_cursor("nori-51.json"), ledger=ledger,
            attention=FakeAttention(seen=True, delay=10.0), clock=lambda: now[0])
        aster.poll_once()
        nori.poll_once()
        self.assertEqual((aster_client.sent, nori_client.sent), ([], []))

        now[0] += 10.0
        nori.poll_once()
        self.assertEqual(len(nori_client.sent), 1)
        aster_client.messages.append({
            "id": "nori-reply", "created_at": now[0],
            "author": {"id": "nori", "kind": "entity"},
            "parts": [{"kind": "text", "text": "ノリが先に答えた"}]})
        now[0] += 20.0
        aster.poll_once()
        self.assertEqual(len(aster_client.sent), 1, "别人先回复不能取消她对主人那句的回应")
        call = next(row for row in self.calls
                    if row["request_id"] == "msg_51" and row["from"] == "aster")
        self.assertIn("ノリが先に答えた", call["room_note"],
                      "到期时取的房间前文必须包含等待期间的新回复")

    def test_newer_own_reservation_cancels_old_delayed_turn(self):
        now = [6000.0]
        ledger = GroupLedger(self.root / "newer-own.json", clock=lambda: now[0])
        msg = event(cursor="61", text="旧消息", conversation_kind="group", created_at=now[0])
        client = FakeClient([msg])
        adapter = self.adapter(
            client, "aster", ledger=ledger, attention=FakeAttention(seen=True, delay=30.0),
            clock=lambda: now[0])
        adapter.poll_once()
        self.assertTrue(ledger.reserve("cnv_1", "msg_new", "aster", at=now[0] + 21,
                                       from_ai=False, owner=True)[0])
        now[0] += 35.0
        adapter.poll_once()
        self.assertEqual(client.sent, [], "她已对更新消息占位，旧消息到期只回一次")
        reservations = ledger._data.get("reservations") or {}
        self.assertNotIn("aster", reservations.get("cnv_1\0msg_61", []))
        self.assertIn("aster", reservations.get("cnv_1\0msg_new", []))

    def test_spectating_a_peer_reply_does_not_cancel_her_reply_to_owner(self):
        """别人先回的那句也会作为事件到她这里；她先以旁观身份读到它，仍要回主人那句（2026-11-16 复审）。

        以前把「她已读到后面」当成放弃条件 ⇒ 每条消息的延迟各自随机，
        只要别人那句的延迟比主人那句短，她看完就一声不吭。
        """
        now = [7000.0]
        ledger = GroupLedger(self.root / "spectate.json", clock=lambda: now[0])
        msg = event(cursor="71", text="主人问大家", event_id="evt_grp_71",
                    conversation_kind="group", created_at=now[0])
        client = FakeClient([dict(msg)])
        client.messages = [dict(msg["payload"]["message"])]
        aster = self.adapter(client, "aster", cursor_path=self.fresh_cursor("aster-71.json"),
                           ledger=ledger, attention=FakeAttention(seen=True, delay=30.0),
                           clock=lambda: now[0])
        aster.poll_once()                                   # 占到名额，30 秒后回
        self.assertIn("aster", ledger._data["reservations"]["cnv_1\0msg_71"])

        now[0] += 10.0                                    # nori 先回了，这句也到 aster 这里
        peer = event("nori", cursor="72", text="ノリが先に答えた", event_id="evt_grp_72",
                     conversation_kind="group", created_at=now[0])
        client.rows.append(peer)
        client.messages.append(dict(peer["payload"]["message"]))
        aster.group_attention = FakeAttention(seen=True, delay=5.0)
        aster.poll_once()
        now[0] += 5.0
        aster.poll_once()                                   # 她瞄了 nori 那句：同人间隔内，只旁观
        self.assertEqual(client.sent, [])
        self.assertGreater(ledger.last_seen("cnv_1", "aster"), 7000.0, "她确实已读到后面")

        now[0] += 15.0                                    # 主人那句到期
        aster.poll_once()
        self.assertEqual(len(client.sent), 1, "只是看了别人的回复，不该放弃回主人")
        self.assertEqual(client.sent[0]["mutation_id"], "agent:msg_71")



if __name__ == "__main__":
    unittest.main(verbosity=2)
