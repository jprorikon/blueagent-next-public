#!/usr/bin/env python3
"""主動消息改走 App Server 會話（docs/PLAN-common-ground-20261109.md §7.4）。

判據：
  ① 主動消息經她自己的帳號寫進主人↔她的 direct 會話；送不出去 ⇒ 退回舊 SSE（不吞掉）。
  ② App Server 的已讀游標**往前走**才記一條 `chat.read.updated`（重複標不記），只給參與者。
  ③ Agent 吃到那條 ⇒ 閘知道「沒讀／讀了沒回」；組句與獨白的那一句跟著變。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403
# 借用 App 測試裡的夾具（App Server 那一側的測試在 app/tests/）
import sys as _sys, pathlib as _pl  # noqa: E401
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "app" / "tests"))

import test_app_server_chat as chat_case  # noqa: E402

from agent.adapter.app_proactive import AppConversationChannel, attach, make_read_handler
from agent.adapter.app_server import AppServerClient
from agent.brain.compose.heartbeat_compose import READ_NOTE, UNANSWERED_NOTE, UNREAD_NOTE, pending_note
from agent.brain.runtime.delivery.delivery import AppChannel, ChannelRouter
from agent.brain.runtime.proactive import ProactiveGate


def gate(**kwargs) -> ProactiveGate:
    return ProactiveGate(min_gap_s=0, daily_max=99, quiet_start=0, quiet_end=0, **kwargs)


class TestEndToEnd(unittest.TestCase):
    """真的起一個 App Server：她送 → 主人那邊看得到 → 主人標已讀 → 她知道。"""

    setUp = chat_case.AppServerChatCase.setUp
    request = chat_case.AppServerChatCase.request

    def events(self, token: str) -> list[dict]:
        status, page = self.request("GET", "/v1/events?after=0&limit=200", token=token)
        self.assertEqual(status, 200, page)
        return list(page.get("events") or [])

    def test_proactive_lands_in_direct_conversation_and_read_flows_back(self) -> None:
        proactive = gate()
        proactive.note_heard("aster", "owner", at=1.0)
        channel = AppConversationChannel({"aster": AppServerClient(self.base, "aster-token")},
                                         on_sent=proactive.note_delivered)
        router = ChannelRouter([AppChannel(bus=None)])
        router.channels[channel.name] = channel
        sent = proactive.maybe_send("aster", "観測表、できたよ", publish=router.send)
        self.assertTrue(sent["sent"], sent)

        status, listing = self.request("GET", "/v1/conversations")
        self.assertEqual(status, 200, listing)
        direct = [row for row in listing["conversations"]
                  if row["kind"] == "direct" and "aster" in row["participants"]]
        self.assertEqual(len(direct), 1, "寫進主人↔她的 direct 會話")
        cid = direct[0]["id"]
        self.assertEqual(direct[0]["unread"], 1, "主人那邊是一則未讀")
        message = direct[0]["last_message"]
        self.assertEqual(message["author"]["id"], "aster")
        self.assertEqual(message["parts"][0]["text"], "観測表、できたよ")
        self.assertEqual(proactive.delivery_state("aster"), "unread")
        self.assertEqual(pending_note(proactive, "aster"), UNREAD_NOTE)

        status, _ = self.request("POST", f"/v1/conversations/{cid}/read",
                                 {"message_id": message["id"]})
        self.assertEqual(status, 200)
        status, _ = self.request("POST", f"/v1/conversations/{cid}/read",
                                 {"message_id": message["id"]})
        self.assertEqual(status, 200)
        reads = [row for row in self.events("aster-token") if row["type"] == "chat.read.updated"]
        self.assertEqual(len(reads), 1, "同一則標兩次只記一條")
        payload = reads[0]["payload"]
        self.assertEqual((payload["account_id"], payload["conversation_id"], payload["message_seq"]),
                         ("owner", cid, message["seq"]))
        self.assertEqual(reads[0]["actor"], {"kind": "owner", "id": "owner"})

        make_read_handler(proactive)("aster", payload)
        self.assertEqual(proactive.delivery_state("aster"), "read")
        self.assertEqual(pending_note(proactive, "aster"), READ_NOTE)
        again = proactive.maybe_send("aster", "観測表ができたから、見てね", publish=router.send)
        self.assertEqual(again["reason"], "repeat_unanswered", "讀了沒回：同一件事不再說一次")

    def test_read_event_is_private_to_participants(self) -> None:
        client = AppServerClient(self.base, "aster-token")
        cid = client.open_direct("owner")["conversation"]["id"]
        message = client.send_message(cid, ["やあ"], mutation_id="m1", causation_id="")["message"]
        self.request("POST", f"/v1/conversations/{cid}/read", {"message_id": message["id"]})
        for token in ("nori-token", "mira-token", "guest-token"):
            self.assertFalse([row for row in self.events(token)
                              if row["type"] == "chat.read.updated"], token)


class FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple] = []
        self.opened: list[str] = []

    def open_direct(self, account_id):
        self.opened.append(account_id)
        return {"conversation": {"id": "cnv_x"}}

    def send_message(self, conversation_id, parts, *, mutation_id, causation_id):
        if self.fail:
            raise RuntimeError("app_server_http_502")
        self.sent.append((conversation_id, list(parts), mutation_id))
        return {"message": {"id": f"msg_{len(self.sent)}", "seq": len(self.sent)}}


class TestChannel(unittest.TestCase):
    def frame(self, **extra):
        return {"kind": "proactive", "entity": "aster", "text": "やあ", "at": 100.0, **extra}

    def test_only_proactive_frames(self) -> None:
        client = FakeClient()
        channel = AppConversationChannel({"aster": client})
        self.assertFalse(channel.send("owner", {"kind": "delivery", "entity": "aster", "text": "x"}))
        self.assertTrue(channel.send("owner", self.frame()))
        self.assertTrue(channel.send("owner", self.frame(at=200.0)))
        self.assertEqual(client.opened, ["owner"], "會話 id 快取，不重開")
        self.assertEqual([row[2] for row in client.sent], ["proactive:aster:100000", "proactive:aster:200000"])

    def test_failure_falls_back(self) -> None:
        channel = AppConversationChannel({"aster": FakeClient(fail=True)})
        self.assertFalse(channel.send("owner", self.frame()), "送不出去 ⇒ False，router 退回 SSE")
        self.assertFalse(channel.send("owner", self.frame(entity="nori")), "沒有她的 token ⇒ False")

    def test_router_prefers_conversation_for_proactive_only(self) -> None:
        router = ChannelRouter([AppChannel(bus=None)])
        router.channels["app_server"] = AppConversationChannel({"aster": FakeClient()})
        self.assertEqual(router.candidates("owner", self.frame())[:2], ["app_server", "app"])
        self.assertNotIn("app_server", router.candidates("owner", {"kind": "delivery", "text": "x"}))

    def test_attach_and_switch(self) -> None:
        class Adapter:
            entity, client, on_read = "aster", FakeClient(), None

        class Accounts:
            def account_id_of(self, principal, *, role=""):
                return {"owner": "acct_owner"}.get(principal, principal)

            def principal_of(self, identity):
                return {"acct_owner": "owner"}.get(identity, identity)

        class App:
            delivery = ChannelRouter([AppChannel(bus=None)])
            proactive = gate()
            accounts = Accounts()

        adapter = Adapter()
        self.assertIsNotNone(attach(App, [adapter], env={}))
        self.assertIn("app_server", App.delivery.channels)
        self.assertIs(App.delivery.channels["app_server"].accounts, App.accounts,
                      "通道要拿到帳號翻譯表（否則生產上 open_direct('owner') 一律 404）")
        self.assertIsNotNone(adapter.on_read)
        self.assertIsNone(attach(App, [adapter], env={"BLUEAGENT_PROACTIVE_VIA_CONVERSATION": "0"}))
        self.assertNotIn("app_server", App.delivery.channels, "關掉 ⇒ 拿掉通道，回舊 SSE")


class TestReadHandler(unittest.TestCase):
    def test_ignores_self_and_group(self) -> None:
        proactive = gate()
        handler = make_read_handler(proactive)
        handler("aster", {"account_id": "aster", "conversation_id": "c", "message_seq": 5})
        handler("aster", {"account_id": "owner", "conversation_kind": "group",
                        "conversation_id": "g", "message_seq": 5})
        self.assertEqual(proactive.snapshot().get("heard"), {})
        self.assertEqual(proactive._read, {}, "自己讀的、群聊的都不算")

    def test_unknown_state_without_platform_record(self) -> None:
        proactive = gate()
        proactive.note_heard("aster", "owner", at=1.0)
        proactive.maybe_send("aster", "やあ", publish=lambda principal, frame: None)
        self.assertEqual(proactive.delivery_state("aster"), "", "舊 SSE 路 ⇒ 不知道讀了沒")
        self.assertEqual(pending_note(proactive, "aster"), UNANSWERED_NOTE)

    def test_state_persists(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "proactive.json")
            first = gate(state_path=path)
            first.note_heard("aster", "owner", at=1.0)
            first.maybe_send("aster", "やあ", publish=lambda principal, frame: first.note_delivered(
                "aster", principal, conversation_id="c", message_id="m", seq=3, at=frame["at"]))
            first.note_read("aster", "owner", conversation_id="c", seq=3)
            second = gate(state_path=path)
            self.assertEqual(second.delivery_state("aster"), "read")


class TestProductionAccountIds(unittest.TestCase):
    """生產形狀：平台帳號 id 是 `acct_owner`，大腦 principal 是 `owner`（2026-11-09 實測的缺口）。

    沒翻這一步時：`open_direct("owner")` 回 `account_not_found` ⇒ 通道靜默降級回舊 SSE；
    `read` 回流的 `account_id` 也被當成 principal，鍵永遠對不上。
    """

    #: 生產的主人帳號 id（`owner` 只是 legacy principal）
    OWNER_ID = "acct_owner"
    OWNER_LEGACY = "owner"
    tmp: object
    base: str

    def setUp(self):
        chat_case.start_app_server(self, owner_id=self.OWNER_ID, owner_legacy=self.OWNER_LEGACY)

    def request(self, method, path, body=None, token="owner-token", headers=None):
        return chat_case.chat_request(self.base, method, path, body=body, token=token,
                                      headers=headers)

    def _accounts(self):
        """大腦的帳號表（`data/accounts.json` 的形狀）：acct_owner ↔ owner。"""
        import hashlib
        import json
        import pathlib

        from agent.brain.runtime.identity.accounts import AccountStore

        path = pathlib.Path(self.tmp.name) / "agent-accounts.json"
        path.write_text(json.dumps({"version": 1, "accounts": {
            "acct_owner": {"account_id": "acct_owner", "role": "master", "principal": "owner",
                           "display": "owner",
                           "token_sha256": hashlib.sha256(b"owner-token").hexdigest()},
        }}), encoding="utf-8")
        return AccountStore(path)

    def test_channel_translates_principal_and_read_flows_back(self) -> None:
        accounts = self._accounts()
        proactive = gate()
        proactive.note_heard("aster", "owner", at=1.0)
        channel = AppConversationChannel({"aster": AppServerClient(self.base, "aster-token")},
                                         accounts=accounts, on_sent=proactive.note_delivered)
        router = ChannelRouter([AppChannel(bus=None)])
        router.channels[channel.name] = channel
        sent = proactive.maybe_send("aster", "観測表、できたよ", publish=router.send)
        self.assertTrue(sent["sent"], sent)
        self.assertEqual(router.stats["sent"], 1, router.stats)
        self.assertEqual(router.stats["failed"], 0, "沒有降級（送不出去才會 +1）")
        self.assertEqual(proactive.delivery_state("aster"), "unread")

        status, listing = self.request("GET", "/v1/conversations")
        self.assertEqual(status, 200, listing)
        direct = [row for row in listing["conversations"]
                  if row["kind"] == "direct" and "aster" in row["participants"]]
        self.assertEqual(len(direct), 1, direct)
        self.assertEqual(direct[0]["participants"], ["acct_owner", "aster"],
                         "會話是主人帳號 id ↔ 她，不是 principal 字串")
        cid = direct[0]["id"]
        message = direct[0]["last_message"]

        # 主人在平台標已讀 ⇒ 事件裡的 account_id 是 acct_owner，閘的鍵要回到 owner
        status, _ = self.request("POST", f"/v1/conversations/{cid}/read",
                                 {"message_id": message["id"]})
        self.assertEqual(status, 200)
        handler = make_read_handler(proactive, accounts=accounts)
        handler("aster", {"account_id": "acct_owner", "conversation_kind": "direct",
                        "conversation_id": cid, "message_seq": message["seq"]})
        self.assertEqual(proactive.delivery_state("aster"), "read")
        self.assertEqual(pending_note(proactive, "aster"), READ_NOTE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
