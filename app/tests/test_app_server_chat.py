#!/usr/bin/env python3
"""S3：App Server 会话、消息、私信事件 ACL、MCP 与 legacy 迁移。"""

from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from typing import Any

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.chat.migration import migrate_outbox
from app.server.config import AppConfig
from app.server.http import make_server


def start_app_server(case, *, owner_id: str = "owner", owner_legacy: str = "") -> None:
    """在 `case` 上起一個臨時 App Server（帳號表可改成生產形狀：`acct_owner` + legacy `owner`）。"""
    case.tmp = tempfile.TemporaryDirectory(prefix="ba-app-chat-")
    case.addCleanup(case.tmp.cleanup)
    root = pathlib.Path(case.tmp.name)
    accounts = root / "accounts.json"
    rows = [(owner_id, "owner", "主人", "owner-token"),
            ("aster", "entity", "アスター", "aster-token"),
            ("nori", "entity", "ノリ", "nori-token"),
            ("mira", "entity", "ミラ", "mira-token")]
    owner_extra = {"legacy_principal": owner_legacy} if owner_legacy else {}
    accounts.write_text(json.dumps({"schema_version": 1, "accounts": [
        {"id": identity, "kind": kind, "display": display,
         "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
         **(owner_extra if kind == "owner" else {})}
        for identity, kind, display, token in rows] + [
        {"id": "acct_guest", "kind": "guest", "display": "輝石",
         "legacy_principal": "active-guest",
         "token_sha256": hashlib.sha256(b"guest-token").hexdigest()},
        {"id": "acct_guest_a", "kind": "guest", "display": "A",
         "legacy_principal": "guesttest",
         "token_sha256": hashlib.sha256(b"guest-a-token").hexdigest()},
        {"id": "acct_guest_b", "kind": "guest", "display": "B",
         "legacy_principal": "guesttest",
         "token_sha256": hashlib.sha256(b"guest-b-token").hexdigest()},
    ], "migration_accounts": [
        {"id": "acct_revoked", "kind": "guest", "display": "Alice",
         "legacy_principal": "guest-707", "token_sha256": "0" * 64},
    ]}), encoding="utf-8")
    case.server = make_server(AppConfig("127.0.0.1", 0, root / "data", accounts))
    case.thread = threading.Thread(target=case.server.serve_forever, daemon=True)
    case.thread.start()
    case.base = f"http://127.0.0.1:{case.server.server_port}"
    case.addCleanup(case.thread.join, 2.0)
    case.addCleanup(case.server.server_close)
    case.addCleanup(case.server.shutdown)


def chat_request(base, method, path, body=None, token="owner-token", headers=None):
    """打臨時 App Server 的一支（回 `(status, json)`）；fixture 與外部測試共用。"""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


class AppServerChatCase(unittest.TestCase):
    #: 主人帳號 id：預設 `owner`；生產是 `acct_owner`（legacy principal 才是 owner）。
    #: 子類／直接呼叫 `start_app_server` 就能得到「生產形狀」的帳號表
    #: （2026-11-09 的 principal／id 分裂就是因為 fixture 一直是 `owner` 才漏掉的）。
    OWNER_ID = "owner"
    OWNER_LEGACY = ""
    tmp: tempfile.TemporaryDirectory
    server: Any
    thread: threading.Thread
    base: str

    def setUp(self):
        # `getattr`：有些測試直接把這個 setUp 掛到自己類別上（沒有 OWNER_ID／OWNER_LEGACY）
        start_app_server(self, owner_id=getattr(self, "OWNER_ID", "owner"),
                         owner_legacy=getattr(self, "OWNER_LEGACY", ""))

    def request(self, method, path, body=None, token="owner-token", headers=None):
        return chat_request(self.base, method, path, body=body, token=token, headers=headers)

    def direct(self):
        status, body = self.request("POST", "/v1/conversations/direct", {"account_id": "aster"})
        self.assertEqual(status, 200, body)
        return body["conversation"]

    def mcp(self, name, arguments, token="aster-token"):
        status, reply = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": name, "arguments": arguments}}, token=token)
        self.assertEqual(status, 200, reply)
        return reply["result"]

    def test_send_is_transactional_idempotent_and_part_scoped(self):
        conversation = self.direct()
        cid = conversation["id"]
        body = {"text": "秘密の一言", "client_mutation_id": "client-1"}
        status, sent = self.request("POST", f"/v1/conversations/{cid}/messages", body)
        self.assertEqual(status, 200, sent)
        status, replay = self.request("POST", f"/v1/conversations/{cid}/messages", body)
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["message"]["id"], sent["message"]["id"])
        self.assertEqual(replay["event_id"], sent["event_id"])
        status, conflict = self.request("POST", f"/v1/conversations/{cid}/messages",
                                       {"text": "別文", "client_mutation_id": "client-1"})
        self.assertEqual((status, conflict["error"]), (409, "mutation_conflict"))

        for token in ("owner-token", "aster-token"):
            status, page = self.request("GET", f"/v1/conversations/{cid}/messages", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(len(page["messages"]), 1, "幂等重试不能写第二条")
            self.assertEqual(page["messages"][0]["parts"][0]["text"], "秘密の一言")
        status, denied = self.request("GET", f"/v1/conversations/{cid}/messages", token="mira-token")
        self.assertEqual((status, denied["error"]), (404, "conversation_not_found"))
        status, denied = self.request("POST", f"/v1/conversations/{cid}/messages",
                                     {"text": "盗み書き"}, token="mira-token")
        self.assertEqual((status, denied["error"]), (404, "conversation_not_found"))

        for token in ("owner-token", "aster-token"):
            _, events = self.request("GET", "/v1/events", token=token)
            self.assertIn("秘密の一言", repr(events))
        _, outsider = self.request("GET", "/v1/events", token="mira-token")
        self.assertNotIn("秘密の一言", repr(outsider))
        self.assertNotIn(cid, repr(outsider))

    def test_mcp_parts_causation_read_cursor_and_paging(self):
        cid = self.direct()["id"]
        listed = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 2,
                                                "method": "tools/list"}, token="aster-token")[1]
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertIn("chat.message.send", names)
        result = self.mcp("chat.message.send", {"conversation_id": cid,
            "parts": [{"kind": "text", "text": "一。"}, {"kind": "text", "text": "二。"}],
            "causation_id": "evt_human", "client_mutation_id": "reply-1"})
        self.assertFalse(result.get("isError"), result)
        sent = json.loads(result["content"][0]["text"])
        self.assertEqual(len(sent["message"]["parts"]), 2)
        _, owner_events = self.request("GET", "/v1/events")
        event = next(row for row in owner_events["events"] if row["type"] == "chat.message.created")
        self.assertEqual(event["causation_id"], "evt_human")

        _, conversations = self.request("GET", "/v1/conversations")
        self.assertEqual(conversations["conversations"][0]["unread"], 1)
        status, read = self.request("POST", f"/v1/conversations/{cid}/read",
                                    {"message_id": sent["message"]["id"]})
        self.assertEqual(status, 200, read)
        self.assertEqual(read["conversation"]["unread"], 0)
        for index in range(3):
            self.request("POST", f"/v1/conversations/{cid}/messages", {"text": f"n{index}"})
        _, page = self.request("GET", f"/v1/conversations/{cid}/messages?limit=2")
        self.assertEqual([row["parts"][0]["text"] for row in page["messages"]], ["n1", "n2"])
        self.assertTrue(page["has_more"])
        _, older = self.request("GET",
            f"/v1/conversations/{cid}/messages?limit=2&before={page['next_before']}")
        self.assertEqual(older["messages"][-1]["parts"][0]["text"], "n0")

    def test_sse_never_delivers_private_event_to_outsider(self):
        cid = self.direct()["id"]
        self.request("POST", f"/v1/conversations/{cid}/messages", {"text": "SSE private"})
        self.mcp("post", {"text": "public after private"}, token="aster-token")
        request = urllib.request.Request(self.base + "/v1/events?after=0", headers={
            "Authorization": "Bearer mira-token", "Accept": "text/event-stream"})
        with urllib.request.urlopen(request, timeout=5) as response:
            frame = {}
            while True:
                line = response.readline().decode().strip()
                if not line:
                    if "data" in frame:
                        if frame.get("event") == "ready":
                            self.assertNotIn("id", frame)
                            frame = {}
                            continue
                        break
                    frame = {}
                    continue
                key, _, value = line.partition(": ")
                frame[key] = value
        self.assertEqual(frame["event"], "post.created")
        self.assertNotIn("SSE private", frame["data"])

    def test_legacy_migration_is_dry_by_default_and_replayable(self):
        index = pathlib.Path(self.tmp.name) / "legacy-index.jsonl"
        rows = [
            {"turn_id": "t1", "principal": "owner", "entity": "aster", "at": 100,
             "source": "phone:owner", "asked": "old", "text": "old reply"},
            {"turn_id": "t1", "principal": "owner", "entity": "aster", "at": 101,
             "source": "phone:owner", "asked": "質問", "text": "一。二。", "parts": ["一。", "二。"]},
            {"turn_id": "t2", "principal": "owner", "entity": "aster", "at": 102,
             "source": "api:image", "asked_parts": [{"kind": "photo", "media_id": "img1"}],
             "text": "写真だね"},
            {"turn_id": "dc", "principal": "owner", "entity": "aster", "source": "discord:owner",
             "asked": "別渠道", "text": "import しない"},
            {"turn_id": "call", "principal": "owner", "entity": "aster", "source": "phone:call",
             "kind": "call-log", "text": "通話 3 分", "call_s": 180},
        ]
        index.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                         encoding="utf-8")
        original = index.read_bytes()
        dry = migrate_outbox(index)
        self.assertTrue(dry["dry_run"])
        self.assertEqual((dry["turns"], dry["messages"], dry["conversations"], dry["skipped"]),
                         (2, 4, 1, 2))
        self.assertEqual(index.read_bytes(), original)
        services = self.server.services
        first = migrate_outbox(index, services.chat, services.accounts, apply=True)
        self.assertEqual(first["imported"], 4, first)
        second = migrate_outbox(index, services.chat, services.accounts, apply=True)
        self.assertEqual(second["imported"], 0, second)
        cid = self.direct()["id"]
        _, page = self.request("GET", f"/v1/conversations/{cid}/messages?limit=20")
        self.assertEqual(len(page["messages"]), 4)
        self.assertNotIn("old reply", repr(page))
        reply = next(row for row in page["messages"] if len(row["parts"]) == 2)
        self.assertEqual([part["text"] for part in reply["parts"]], ["一。", "二。"])
        _, events = self.request("GET", "/v1/events?after=0")
        self.assertFalse(any(row["type"] == "chat.message.created" for row in events["events"]),
                         "历史导入不能触发 Agent 新回复")

    def test_legacy_aliases_are_unique_and_unknown_accounts_stay_unwritten(self):
        index = pathlib.Path(self.tmp.name) / "legacy-aliases.jsonl"
        rows = [
            {"turn_id": "active", "principal": "active-guest", "entity": "aster", "at": 99,
             "source": "phone:active-guest", "asked": "現在", "text": "現在の返答"},
            {"turn_id": "alias", "principal": "guest-707", "entity": "aster", "at": 100,
             "source": "phone:guest-707", "asked": "旧質問", "text": "旧返答"},
            {"turn_id": "duplicate", "principal": "guesttest", "entity": "aster", "at": 101,
             "source": "phone:guesttest", "asked": "重複", "text": "書き込まない"},
            {"turn_id": "unknown", "principal": "test", "entity": "aster", "at": 102,
             "source": "phone:test", "asked": "未知", "text": "書き込まない"},
        ]
        index.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                         encoding="utf-8")
        services = self.server.services
        first = migrate_outbox(index, services.chat, services.accounts, apply=True)
        self.assertEqual((first["imported"], first["conflicts"]), (4, 2), first)
        second = migrate_outbox(index, services.chat, services.accounts, apply=True)
        self.assertEqual((second["imported"], second["conflicts"]), (0, 2), second)
        self.assertEqual(second["idempotent_replays"], 4, second)

        _, conversations = self.request("GET", "/v1/conversations", token="guest-token")
        self.assertEqual(len(conversations["conversations"]), 1)
        cid = conversations["conversations"][0]["id"]
        _, page = self.request("GET", f"/v1/conversations/{cid}/messages", token="guest-token")
        self.assertEqual(len(page["messages"]), 2)
        self.assertEqual({row["author"]["id"] for row in page["messages"]}, {"acct_guest", "aster"})
        revoked = services.chat_store._db.execute(
            "SELECT COUNT(*) FROM message WHERE conversation_id IN "
            "(SELECT conversation_id FROM conversation_participant WHERE account_id='acct_revoked')"
        ).fetchone()[0]
        self.assertEqual(revoked, 2, "抹消済みアカウントの履歴は元のIDに保存する")
        status, _ = self.request("GET", "/v1/conversations", token="revoked-token")
        self.assertEqual(status, 401, "履歴専用アカウントを認証へ復活させない")
        _, events = self.request("GET", "/v1/events?after=0", token="guest-token")
        self.assertFalse(any(row["type"] == "chat.message.created" for row in events["events"]))

    def test_owner_guest_dm_is_idempotent_and_invisible_to_agents(self):
        status, opened = self.request(
            "POST", "/v1/conversations/direct", {"account_id": "acct_guest"})
        self.assertEqual(status, 200, opened)
        conversation = opened["conversation"]
        cid = conversation["id"]
        self.assertEqual(conversation["kind"], "direct")
        self.assertEqual(set(conversation["participants"]), {"owner", "acct_guest"})

        # P-C2：列表附成员的 kind／display，客户端才分得出「人」与角色、画得出客人的名字
        _, listed = self.request("GET", "/v1/conversations")
        row = next(item for item in listed["conversations"] if item["id"] == cid)
        self.assertEqual({(m["id"], m["kind"], m["display"]) for m in row["members"]},
                         {("owner", "owner", "主人"), ("acct_guest", "guest", "輝石")})

        status, reversed_open = self.request(
            "POST", "/v1/conversations/direct", {"account_id": "owner"}, token="guest-token")
        self.assertEqual(status, 200, reversed_open)
        self.assertEqual(reversed_open["conversation"]["id"], cid)
        status, denied = self.request(
            "POST", "/v1/conversations/direct", {"account_id": "acct_guest_b"}, token="guest-a-token")
        self.assertEqual((status, denied["error"]), (403, "direct_requires_owner"))

        status, sent = self.request(
            "POST", f"/v1/conversations/{cid}/messages",
            {"text": "owner guest only", "client_mutation_id": "guest-dm-1"}, token="guest-token")
        self.assertEqual(status, 200, sent)
        for token in ("owner-token", "guest-token"):
            status, page = self.request("GET", f"/v1/conversations/{cid}/messages", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(page["messages"][0]["parts"][0]["text"], "owner guest only")
            _, events = self.request("GET", "/v1/events?after=0", token=token)
            event = next(row for row in events["events"] if row["type"] == "chat.message.created")
            self.assertEqual(event["payload"]["conversation_kind"], "direct")
            self.assertEqual(event["payload"]["message"]["id"], sent["message"]["id"])

        for token in ("aster-token", "nori-token", "mira-token", "guest-a-token"):
            status, denied = self.request("GET", f"/v1/conversations/{cid}/messages", token=token)
            self.assertEqual((status, denied["error"]), (404, "conversation_not_found"))
            status, denied = self.request(
                "POST", f"/v1/conversations/{cid}/messages", {"text": "not allowed"}, token=token)
            self.assertEqual((status, denied["error"]), (404, "conversation_not_found"))
            _, events = self.request("GET", "/v1/events?after=0", token=token)
            self.assertNotIn("owner guest only", repr(events))
            self.assertNotIn(cid, repr(events))
            _, listed = self.request("GET", "/v1/conversations", token=token)
            self.assertNotIn(cid, {row["id"] for row in listed["conversations"]})

    def test_fixed_agent_room_uses_shared_group_and_participant_events(self):
        status, denied = self.request("POST", "/v1/conversations/agent-room", {}, token="guest-token")
        self.assertEqual((status, denied["error"]), (403, "agent_room_forbidden"))
        status, opened = self.request("POST", "/v1/conversations/agent-room", {})
        self.assertEqual(status, 200, opened)
        room = opened["conversation"]
        self.assertEqual(room["id"], "cnv_agent_room")
        self.assertEqual(room["kind"], "group")
        self.assertEqual(set(room["participants"]), {"owner", "aster", "nori", "mira"})
        self.assertNotIn("acct_revoked", room["participants"])

        via_mcp = self.mcp("chat.agent_room.open", {}, token="aster-token")
        self.assertFalse(via_mcp.get("isError"), via_mcp)
        self.assertEqual(json.loads(via_mcp["content"][0]["text"])["conversation"]["id"], room["id"])
        status, sent = self.request(
            "POST", f"/v1/conversations/{room['id']}/messages",
            {"text": "shared group message", "client_mutation_id": "group-1"})
        self.assertEqual(status, 200, sent)

        for token in ("owner-token", "aster-token", "nori-token", "mira-token"):
            status, page = self.request("GET", f"/v1/conversations/{room['id']}/messages", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(page["messages"][0]["id"], sent["message"]["id"])
            _, events = self.request("GET", "/v1/events?after=0", token=token)
            event = next(row for row in events["events"] if row["type"] == "chat.message.created")
            self.assertEqual(event["payload"]["conversation_kind"], "group")
            self.assertEqual(set(event["payload"]["participants"]),
                             {"owner", "aster", "nori", "mira"})
            self.assertEqual(event["payload"]["message"]["id"], sent["message"]["id"])

        for token in ("guest-token", "guest-a-token"):
            status, denied = self.request(
                "GET", f"/v1/conversations/{room['id']}/messages", token=token)
            self.assertEqual((status, denied["error"]), (404, "conversation_not_found"))
            _, events = self.request("GET", "/v1/events?after=0", token=token)
            self.assertNotIn("shared group message", repr(events))
            self.assertNotIn(room["id"], repr(events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
