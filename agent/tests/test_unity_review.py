#!/usr/bin/env python3
"""unity-review.md（2026-10-28）的回歸：服務端那一半＋ Next 客戶端的靜態守門。

  · U-1：事件編號跨重啟仍單調（重啟前的大游標不能把重啟後的新訊息濾掉）；
  · U-4：推到 App 的主動訊息／後續交付落進 outbox，帶 `turn_id`，換角色後 `/api/turns` 補得回來；
  · 客戶端（C#）的規則本體在 `app/client/shared/BlueAgent.Logic`（`NextSessionLogicTest`），
    這裡只守「視圖層有沒有接上」——以前的寫法一出現就失敗。
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.bootstrap import build
from agent.brain.compose.followups import VoiceTurns
from agent.brain.compose.state import AppState
from agent.brain.runtime.delivery.bus import EventBus
from agent.brain.runtime.delivery.outbox import TurnOutbox
from agent.server.api.turns import list_turns

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
NEXT = ROOT / "app" / "client" / "BlueAgentUnity" / "Assets" / "Scripts" / "Next"


def _source(name: str) -> str:
    return (NEXT / name).read_text(encoding="utf-8")


def _code(name: str) -> str:
    """去掉 `//` 註解（含 `///` 文件註解）——規則看的是程式，不是說明。"""
    return "\n".join(line.split("//", 1)[0] for line in _source(name).splitlines())


class TestEventIdsSurviveRestart(unittest.TestCase):
    def test_ids_after_restart_are_larger_than_before(self):
        """★ 以前從 1 起算：App 帶著重啟前的 5 回來，重啟後的 1..4 全被 `id > 5` 濾掉。"""
        before = EventBus(clock=lambda: 1_000.0)
        for index in range(5):
            before.publish("owner", {"kind": "delivery", "text": str(index)})
        cursor = before.last_id("owner")

        after = EventBus(clock=lambda: 1_060.0)           # 一分鐘後重啟
        after.publish("owner", {"kind": "delivery", "text": "重啟後"})
        self.assertGreater(after.last_id("owner"), cursor)
        self.assertEqual([row["text"] for row in after.replay("owner", cursor)], ["重啟後"])

    def test_replay_still_per_principal(self):
        bus = EventBus()
        bus.publish("guest-x", {"kind": "delivery", "text": "guest の"})
        guest_cursor = bus.last_id("guest-x")
        bus.publish("owner", {"kind": "delivery", "text": "owner の"})
        bus.publish("guest-x", {"kind": "delivery", "text": "guest 二つ目"})
        self.assertEqual([row["text"] for row in bus.replay("guest-x", guest_cursor)], ["guest 二つ目"])


class TestDeliveriesLandInOutbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.outbox = TurnOutbox(self.tmp.name)
        self.bus = EventBus(recorder=self.outbox.record_delivery)

    def tearDown(self):
        self.tmp.cleanup()

    def test_proactive_frame_gets_turn_id_and_row(self):
        sub_id, channel = self.bus.subscribe("owner")
        self.bus.publish("owner", {"kind": "proactive", "entity": "aster", "text": "ねえ、起きてる？"})
        envelope = channel.get(timeout=1.0)
        self.bus.unsubscribe("owner", sub_id)
        turn_id = envelope.get("turn_id")
        self.assertTrue(turn_id, "推播要帶 turn_id，客戶端才去得了重")
        row = self.outbox.get(turn_id, principal="owner")
        self.assertIsNotNone(row)
        self.assertEqual(row["entity"], "aster")
        self.assertEqual(row["text"], "ねえ、起きてる？")
        self.assertTrue(row["source"].startswith("phone"), "App 的歷史只認 phone／api 開頭")
        self.assertEqual(self.bus.replay("owner", 1)[-1]["turn_id"], turn_id, "重播的那份也帶同一個 id")

    def test_control_frames_and_own_turns_are_not_recorded(self):
        self.bus.publish("owner", {"kind": "trpg_closed", "entity": "aster", "reason": "quit"})
        self.bus.publish("owner", {"kind": "delivery", "entity": "aster", "text": "既に記録済み",
                                   "turn_id": "t1-0001"})
        self.bus.publish("owner", {"kind": "delivery", "text": "角色なし"})
        self.assertEqual(self.outbox.since("", principal="owner"), [])

    def test_recorder_failure_does_not_block_push(self):
        def broken(_principal, _frame):
            raise OSError("disk full")

        bus = EventBus(recorder=broken)
        sub_id, channel = bus.subscribe("owner")
        self.assertEqual(bus.publish("owner", {"kind": "proactive", "entity": "aster", "text": "やあ"}), 1)
        self.assertEqual(channel.get(timeout=1.0)["text"], "やあ")
        bus.unsubscribe("owner", sub_id)


class TestDeliverySourceStaysWithItsChannel(unittest.TestCase):
    """複檢 2026-10-28：交付落 outbox 時要照**它原本的來源**分端（owner 2026-10-27「App 與 DC 各自獨立」）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.outbox = TurnOutbox(self.tmp.name)
        self.bus = EventBus(recorder=self.outbox.record_delivery)
        self.auth = types.SimpleNamespace(principal="owner")
        self.state = types.SimpleNamespace(app=types.SimpleNamespace(outbox=self.outbox))

    def tearDown(self):
        self.tmp.cleanup()

    def _app_history(self, entity="mira"):
        _status, body = list_turns(self.state, {"entity": [entity], "source": ["phone,api"]}, auth=self.auth)
        return [row["text"] for row in body["turns"]]

    def _voice_turns(self, sent):
        def send(text, **kwargs):
            sent.append(kwargs)
            body = "ちょっと待ってね" if kwargs.get("quick") else "調べ終わったよ"
            return {"ok": True, "frames": [{"kind": "done", "payload": {"text": body}}]}

        commands = mock.Mock()
        commands.submit.return_value = {"ok": True, "id": "c1"}
        return VoiceTurns(send=send, commands=commands, bus=self.bus), commands

    def test_discord_vc_followup_stays_out_of_app_history(self):
        """★ 以前一律記成 `phone:delivery` ⇒ Discord 語音頻道交代的工作，做完出現在 App 的對話裡。"""
        turns, _commands = self._voice_turns([])
        turns.publish({"text": "調べ終わったよ", "decision": {}}, entity="mira",
                      principal="owner", source="discord:vc")
        self.assertEqual(self._app_history(), [])
        rows = self.outbox.since("", principal="owner")
        self.assertEqual([row["source"] for row in rows], ["discord:vc"], "照樣落盤，只是記在 Discord 那一端")

    def test_proactive_without_source_is_app_side(self):
        self.bus.publish("owner", {"kind": "proactive", "entity": "mira", "text": "ねえ"})
        self.assertEqual(self._app_history(), ["ねえ"])

    def test_app_voice_work_followup_is_app_side(self):
        """★ `/api/audio`＋`X-Mode: work` 把來源放在 `source_override`：以前被丟掉、退回 `discord:vc`。"""
        sent = []
        turns, commands = self._voice_turns(sent)
        turns.send_voice("調べて", entity="mira", principal="owner", mode="work", ack_first=True,
                         source_override="api:audio")
        self.assertEqual(sent[0]["source_override"], "api:audio", "短回覆那一輪也要標成 App 的語音")
        self.assertEqual(commands.submit.call_args.kwargs["source"], "api:audio")
        on_done = commands.submit.call_args.kwargs["on_done"]
        on_done({"result": {"frames": [{"kind": "done", "payload": {"text": "三件あった"}}]}})
        self.assertEqual(self._app_history(), ["三件あった"])

    def test_discord_voice_default_is_unchanged(self):
        sent = []
        turns, commands = self._voice_turns(sent)
        turns.send_voice("調べて", mode="work", ack_first=True)
        self.assertEqual(sent[0]["source_override"], "discord:vc")
        self.assertEqual(commands.submit.call_args.kwargs["source"], "discord:vc")


class TestSwitchingEntityRecoversProactive(unittest.TestCase):
    """U-4 端到端：真的 app（bootstrap 接線）→ B 的主動訊息 → 以 App 的查法取 B 的歷史。"""

    def setUp(self):
        self.app = build(brain_delay=0, data_dir=tempfile.mkdtemp(prefix="ba-unity-review-"),
                         step_duration=0.02, life_pause=0.005)
        self.state = AppState(self.app)

    def tearDown(self):
        self.app.shutdown_lives()

    def test_aster_proactive_is_in_aster_history(self):
        self.app.bus.publish("owner", {"kind": "proactive", "entity": "aster", "text": "オーナー、まだ？"})
        auth = types.SimpleNamespace(principal="owner")
        status, body = list_turns(self.state, {"entity": ["aster"], "source": ["phone,api"],
                                               "limit": ["50"]}, auth=auth)
        self.assertEqual(status, 200, body)
        self.assertEqual([row["text"] for row in body["turns"]], ["オーナー、まだ？"])
        status, body = list_turns(self.state, {"entity": ["mira"], "source": ["phone,api"]},
                                  auth=auth)
        self.assertEqual(body["turns"], [], "不串角色")


class TestNextClientWiring(unittest.TestCase):
    """C# 視圖層的靜態守門（規則本體在 BlueAgent.Logic，這裡看有沒有接上）。"""

    def test_u1_cursor_is_partitioned(self):
        api = _code("NextApi.cs")
        self.assertFalse('PlayerPrefs.GetString("next.lastEventId"' in api, "整台一顆的全域游標")
        self.assertTrue("EventCursors" in api)
        self.assertIsNotNone(re.search(r"Cursors\.Bind\(", _code("NextShell.cs")))

    def test_u2_turn_callbacks_carry_their_own_ticket(self):
        chat = _code("NextChatView.cs")
        self.assertFalse("_turnEntity" in chat, "共用的可變欄位拿來判斷回呼來源")
        self.assertTrue("TurnTickets" in chat)
        api = _code("NextApi.cs")
        self.assertIsNone(re.search(r"(?m)^\s*_turnRequest\s*=\s*null\s*;", api), "舊協程無條件清掉新一輪的句柄")

    def test_u3_location_loop_follows_login(self):
        shell = _code("NextShell.cs")
        start = shell[shell.index("private void Start()"):]
        start = start[:start.index("\n        }\n")]
        self.assertFalse("NextLocation.Loop" in start, "未登入就開始讀位置")
        show_login = shell[shell.index("private void ShowLogin("):]
        show_login = show_login[:show_login.index("\n        }\n")]
        self.assertTrue("StopLocation()" in show_login)
        self.assertIsNotNone(re.search(r'Api\.Token\s*=\s*""', show_login))

    def test_u3_logout_aborts_inflight_location_request(self):
        """只停迴圈不夠：已發出的 POST 也必須能 Abort，且舊收尾不能清新請求。"""
        shell = _code("NextShell.cs")
        stop = shell[shell.index("private void StopLocation("):]
        stop = stop[:stop.index("\n        }\n")]
        self.assertTrue("Api.CancelLocation()" in stop)
        self.assertTrue("api.PostLocation(" in _code("NextLocation.cs"))
        api = _code("NextApi.cs")
        cancel = api[api.index("public void CancelLocation("):]
        cancel = cancel[:cancel.index("\n        }\n")]
        self.assertTrue("_locationRequest.Take()" in cancel)
        self.assertTrue(".Abort()" in cancel)
        self.assertTrue("owner?.Set(request)" in api)
        self.assertTrue("owner?.ClearIf(request)" in api)

    def test_request_slots_are_cleared_in_finally(self):
        """複檢 2026-10-28：Set 之後、ClearIf 之前丟例外 ⇒ 槽裡留著已 Dispose 的請求，下一次取消就炸。"""
        lines = [line.strip() for line in _code("NextApi.cs").splitlines()]
        sets = [i for i, line in enumerate(lines) if re.search(r"\.Set\(request\);$", line)]
        self.assertGreaterEqual(len(sets), 5, "turn／audio／image／一般請求／事件都要走擁有者槽")
        for index in sets:
            following = next(line for line in lines[index + 1:] if line)
            self.assertEqual(following, "try", f"第 {index + 1} 行的 Set 之後要馬上進 try")
            clear = next(i for i in range(index + 1, len(lines)) if "ClearIf(request)" in lines[i])
            self.assertIn("finally", lines[index:clear], f"第 {index + 1} 行那個請求的 ClearIf 要在 finally 裡")
        self.assertIsNone(re.search(r"(?m)^\s*_eventsRequest\s*=\s*(request|null)", _code("NextApi.cs")), "事件連線也不准裸指派")

    def test_aggregation_is_invalidated_on_entity_switch_and_logout(self):
        chat = _code("NextChatView.cs")
        reset = chat[chat.index("private void ResetForEntity("):]
        reset = reset[:reset.index("\n        }\n")]
        self.assertIn("CancelPendingAggregation()", reset)
        self.assertIn("AggPump(_aggEpoch)", chat)
        self.assertIn("while (epoch == _aggEpoch", chat)
        self.assertIn("if (epoch == _aggEpoch) _aggPumping = false;", chat,
                      "舊聚合泵不能清新代次的旗標")
        shell = _code("NextShell.cs")
        show_login = shell[shell.index("private void ShowLogin("):]
        show_login = show_login[:show_login.index("\n        }\n")]
        self.assertIn("_chat.CancelPendingAggregation()", show_login)

    def test_media_uploads_follow_session_and_are_aborted_on_logout(self):
        api = _code("NextApi.cs")
        upload = api[api.index("private IEnumerator UploadMediaRoutine("):]
        upload = upload[:upload.index("\n        }\n")]
        self.assertIn("var epoch = Session.Epoch", upload)
        self.assertIn("if (!Settle(status, epoch)) yield break;", upload)
        self.assertIn("_mediaRequests.Add(request)", upload)
        self.assertRegex(upload, r"finally\s*\{\s*_mediaRequests\.Remove\(request\);")
        cancel = api[api.index("public void CancelMediaUploads("):]
        cancel = cancel[:cancel.index("\n        }\n")]
        self.assertIn(".Abort()", cancel)
        shell = _code("NextShell.cs")
        show_login = shell[shell.index("private void ShowLogin("):]
        show_login = show_login[:show_login.index("\n        }\n")]
        self.assertIn("Api.CancelMediaUploads()", show_login)

    def test_s4d_platform_media_uses_v1_and_original_cache_expires(self):
        api = _code("NextApi.cs")
        chat = _code("NextChatView.cs")
        album = _code("NextAlbumView.cs")
        self.assertIn("/v1/media", api)
        for source in (api, chat, album):
            self.assertNotIn("/api/media", source, "平台照片不再依賴 Agent 舊媒體路由")
        cache = (ROOT / "app/client/shared/BlueAgent.Logic/SyncCache.cs").read_text(encoding="utf-8")
        self.assertIn("OriginalMediaTtlSeconds = 7 * 24 * 60 * 60", cache)
        self.assertIn('StartsWith("/api/media/"', cache, "只為舊原圖快取保留過渡清理")
        self.assertIn('StartsWith("/v1/media/"', cache)
        self.assertIn('part == "w=192" || part == "w=360"', cache)

    def test_u4_other_entity_delivery_is_kept(self):
        chat = _code("NextChatView.cs")
        self.assertTrue("ConversationStore" in chat)
        self.assertTrue(".Take(" in chat, "切到那個角色時要交出收著的話")
        self.assertIn("PersistConversations();", chat, "交付必須先落有界快照，App 重啟後仍在")
        self.assertIn("if (string.IsNullOrEmpty(from)) from = _entityId;", chat,
                      "舊版無 entity 文字幀也要走未讀 store，不能在離頁時直接 Append")
        shell = _code("NextShell.cs")
        self.assertIn("_chat.SetChatVisible(false)", shell,
                      "離開聊天頁後，同角色的交付也必須算未讀")

    def test_u4_stashed_render_commits_read_only_after_append(self):
        """Append 中途拋例外時：未讀仍留著，已成功的 turn 才進 shown，重試可續渲染。"""
        chat = _code("NextChatView.cs")
        show = chat[chat.index("private void ShowStashed("):]
        show = show[:show.index("\n        }\n")]
        self.assertIn("_conversations.Preview(_entityId)", show)
        self.assertLess(show.index("Append(item.Text"), show.index("_shownTurns.Add(item.TurnId)"),
                        "Append 成功前不能記 shown，否則同進程重試會跳過失敗訊息")
        self.assertLess(show.index("_shownTurns.Add(item.TurnId)"), show.index("_conversations.Take(_entityId)"),
                        "整批渲染成功後才清未讀")
        self.assertLess(show.index("_conversations.Take(_entityId)"), show.index("PersistConversations();"),
                        "記憶體已讀與持久化快照要一起推進")

    def test_u5_token_is_not_a_static_string(self):
        accounts = _code("NextAccountsView.cs")
        self.assertIsNone(re.search(r"static\s+string\s+_issuedToken", accounts))
        self.assertTrue("BeginVisit()" in accounts)

    def test_u6_401_goes_through_one_gate(self):
        chat = _code("NextChatView.cs")
        self.assertFalse('Contains("401")' in chat, "錯誤原文裡出現 401 就被當成 token 失效")
        api = _code("NextApi.cs")
        self.assertTrue("ReportStatus(" in api)
        self.assertTrue("Unauthorized" in api)

    def test_u7_sprites_have_owners(self):
        offenders = [path.name for path in sorted(NEXT.glob("*.cs"))
                     if path.name != "NextUi.cs" and "Sprite.Create(" in _code(path.name)]
        self.assertEqual(offenders, [], "Sprite.Create 只准在 NextUi 的擁有者 helper 裡")

    def test_u8_and_strings_gate(self):
        result = subprocess.run([sys.executable, str(ROOT / "app" / "client" / "tools" / "check_next_strings.py")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_u8_gate_catches_voice_language_in_ui(self):
        sys.path.insert(0, str(ROOT / "app" / "client" / "tools"))
        try:
            import check_next_strings
        finally:
            sys.path.pop(0)
        bad = 'SetHint(NextStrings.Get("thinking", NextConfig.Language));'
        self.assertTrue(check_next_strings.language_misuse({"X.cs": bad}))
        ok = 'SetHint(NextStrings.Get("thinking"));\nApi.SendAudio(wav, e, NextConfig.Language, v, f, c);'
        self.assertFalse(check_next_strings.language_misuse({"X.cs": ok}))

    def test_m1_session_list_uses_shared_chat_and_local_unread(self):
        listing = _code("NextChatListView.cs")
        self.assertIn("ChatSync.SyncConversations", listing)
        self.assertIn("Api.Chat.Conversations()", listing)
        self.assertNotIn('Api.Get("/api/sessions"', listing,
                         "S3 會話權威是 App Server，不再是 Agent session 聚合")
        self.assertIn("Chat.UnreadFor(entity)", listing)
        self.assertIn("SwitchEntity(entity, backendName)", listing,
                      "显示名只用于画面，切换私聊仍要保留服务端角色名")
        self.assertNotIn("BuildBubble", listing)
        chat = _code("NextChatView.cs")
        self.assertEqual(chat.count("RectTransform BuildBubble("), 1,
                         "會話列表不能長出第二份聊天泡泡實作")
        self.assertIn('IconButton("ChatListBack"', chat)

    def test_s4_group_entry_chat_and_event_route_stay_decoupled_from_direct(self):
        listing = _code("NextChatListView.cs")
        self.assertIn("ChatSync.SyncAgentRoom", listing)
        self.assertIn('Section("ChatListGroups"', listing)
        self.assertIn("ChatRepository.GroupKey(session.OptString(\"id\"))", listing)
        self.assertIn("_shell.SwitchGroup(entity, name)", listing)
        self.assertNotIn("BuildBubble", listing)

        shell = _code("NextShell.cs")
        switch = shell[shell.index("public void SwitchGroup("):]
        switch = switch[:switch.index("\n        }\n")]
        self.assertIn("ShowChat();", switch)
        self.assertNotIn("NextConfig.Entity", switch)
        self.assertNotIn("ResumeSync()", switch)

        sync = _code("NextChatSync.cs")
        group_sync = sync[sync.index('existing?.OptString("kind") == "group"'):]
        group_sync = group_sync[:group_sync.index('_api.Post("/v1/conversations/direct"')]
        self.assertIn("syncKnown(id)", group_sync)
        self.assertNotIn("conversations/direct", group_sync)

        chat = _code("NextChatView.cs")
        self.assertEqual(chat.count("RectTransform BuildBubble("), 1)
        self.assertRegex(chat, r"if \(!noStage\)\s*\{\s*try \{ stage = NextStageView\.Build")
        self.assertIn("var noStage = group || shell.IsPersonChat;", chat, "人与人的私聊同样没有舞台")
        self.assertIn("var allowVoice = !ChatRepository.IsGroupKey(_entityId)", chat)
        self.assertIn("AuthorId = authorId", chat)
        self.assertIn("BuildBubble(msg, msg.Note ?? \"\")", chat)
        self.assertIn("NextBoardUi.Face(\"MsgFace\", row, _shell, authorId", chat)
        group_event = chat[chat.index('conversation?.OptString("kind") == "group"'):]
        group_event = group_event[:group_event.index('var entity = message.OptJObject("author")')]
        self.assertIn("ChatRepository.GroupKey(conversationId)", group_event)
        self.assertIn("PaintLocalLatest(group)", group_event)
        self.assertIn("return;", group_event)
        self.assertNotIn("_conversations.Accept", group_event)

    def test_m1_entry_switch_keeps_legacy_fallback_and_pill_chat(self):
        config = _code("NextConfig.cs")
        self.assertIn("KeyChatList", config)
        self.assertRegex(config, r"bool ChatList[\s\S]*GetInt\(KeyChatList, 1\) != 0")
        shell = _code("NextShell.cs")
        # 通話／相簿收掉回**那一個聊天**；沒有聊天對象才回列表（2026-11-08：回列表 ⇒ 撥號失敗的理由沒地方顯示）
        self.assertIn("if (!string.IsNullOrEmpty(EntityId) || !NextConfig.ChatList) ShowChat(); else ShowChatList();",
                      shell)
        switch = shell[shell.index("public void SwitchEntity("):]
        switch = switch[:switch.index("\n        }\n")]
        self.assertIn("ShowChat();", switch)
        self.assertNotIn("ShowChatList();", switch)
        self.assertNotIn("GuestChat", shell + _code("NextChatListView.cs"))
        self.assertNotIn("RoomChat", shell + _code("NextChatListView.cs"))
        # owner 2026-11-07：1:1 不再放角色切换；只有关掉会话列表的回退模式保留。
        chat = _code("NextChatView.cs")
        self.assertIn("if (!group && !NextConfig.ChatList && entities != null && entities.Length() > 1)", chat)

    def test_m3_feed_home_preserves_m1_fallback_and_chat_navigation(self):
        config = _code("NextConfig.cs")
        self.assertRegex(config, r"bool HomeIsFeed[\s\S]*GetInt\(KeyHomeIsFeed, 1\) != 0")
        shell = _code("NextShell.cs")
        home = shell[shell.index("public void ShowHome()"):]
        home = home[:home.index("\n        }\n")]
        self.assertIn("if (NextConfig.HomeIsFeed) ShowFeed();", home)
        self.assertIn("else if (NextConfig.ChatList) ShowChatList();", home)
        self.assertIn("else ShowChat();", home)
        listing = _code("NextChatListView.cs")
        self.assertIn("NextConfig.HomeIsFeed ? shell.ShowFeed", listing)
        switch = shell[shell.index("public void SwitchEntity("):]
        switch = switch[:switch.index("\n        }\n")]
        self.assertIn("ShowChat();", switch)
        # owner 2026-11-07：公共区不分角色——没有舞台、没有角色切换。
        feed = _code("NextFeedView.cs")
        self.assertNotIn("SwitchFeedEntity", shell + feed)
        self.assertNotIn("NextStageView.Build", feed)
        self.assertNotIn("FeedEntity", feed)
        show_feed = shell[shell.index("public void ShowFeed()"):]
        show_feed = show_feed[:show_feed.index("\n        }\n")]
        self.assertIn("Stage = null;", show_feed)
        # 拉取失败不能被本地副本盖住，变成「今天没有动态」。
        self.assertIn("if (RenderLocal()) ShowFetchError(reply);", feed)

    def test_profile_page_rules(self):
        """个人主页（owner 2026-11-08）：点头像进；角色＝舞台＋心情、人＝横幅＋心情；跨天履历；
        角色有 1:1 按钮；只有自己的主页才有投稿；详情的返回回到来处。"""
        board = _code("NextBoardUi.cs")
        heading = board[board.index("public static RectTransform Heading("):]
        heading = heading[:heading.index("\n        }\n")]
        self.assertIn("OpensProfile(avatar, shell, post);", heading)
        self.assertIn("shell.ShowProfile(who)", board)
        self.assertIn("NextBoardUi.PostCard(\"FeedPost\"", _code("NextFeedView.cs"), "公共区与主页共用一张卡")

        profile = _code("NextProfileView.cs")
        self.assertIn("NextStageView.Build(page, id", profile)
        self.assertIn("NextMoodBadge.Attach(banner, _shell, AccountId);", profile)
        self.assertIn('"backgrounds/street"', profile)
        self.assertIn("NextBoardPaths.AuthorFeed(AccountId, PageSize", profile)
        self.assertNotIn("since", profile.split("private void Load(")[1].split("private void ClearRows")[0],
                         "履历跨天：不按今天过滤")
        self.assertIn("if (IsSelf(shell)) BuildCompose(page);", profile)
        self.assertIn("if (IsSelf(_shell) || !(IsEntity || CanDirect)) return;", profile)
        self.assertIn("return _shell.IsMaster ? kind == \"guest\" : kind == \"owner\";", profile,
                      "人与人只开主人↔客人（客人之间不互通）")
        self.assertIn("_shell.OpenDirect(id, kind, name)", profile)
        self.assertIn("_shell.SwitchEntity(id", profile)
        self.assertIn('"&authors="', _code("NextBoardPaths.cs"))

        shell = _code("NextShell.cs")
        show = shell[shell.index("public void ShowProfile("):]
        show = show[:show.index("\n        }\n")]
        self.assertIn("var back = _here ?? ShowFeed;", show)
        self.assertIn("_profile.AccountId == who.OptString(\"author_id\")", show, "主页里点同一人不重复开")
        self.assertIn("if (!keepBack || PostBack == null) PostBack = _here ?? ShowFeed;", shell)
        self.assertIn("_here = () => ShowPost(id, true);", shell, "从主页返回详情时沿用详情原本的返回目标")
        self.assertIn("shell.PostBack ?? shell.ShowFeed", _code("NextPostView.cs"))

    def test_person_direct_messages_p_c2(self):
        """P-C2：主人↔客人私聊。会话列表按 members 分出「人」；聊天页没有舞台／语音／通话；头像按人画。"""
        listing = _code("NextChatListView.cs")
        self.assertIn("RememberMembers(row);", listing)
        self.assertIn("else if (_shell.IsAgent(Counterpart(row))) agents.Put(row);", listing)
        self.assertIn('NextStrings.Get("sessions_people")', listing)
        self.assertIn("NextBoardUi.Face(\"SessionAvatar_\"", listing)
        chat = _code("NextChatView.cs")
        self.assertIn("&& !(_shell != null && _shell.IsPersonChat);", chat, "语音消息走 Agent，人的私聊不给")
        self.assertIn("!shell.IsPersonChat && shell.IsMaster", chat, "通话是跟角色的")
        self.assertIn("NextBoardUi.Face(\"MsgFace\", row, _shell, authorId", chat)
        shell = _code("NextShell.cs")
        self.assertIn("public void OpenDirect(string id, string kind, string display)", shell)
        self.assertIn("if (IsAgent(id)) NextConfig.Entity = id;", shell, "人的私聊不记成上次的角色")

    def test_outbox_results_reach_the_screen(self):
        """2026-11-08 实测：服务端早已收下，回复仍挂「送信待ち」、主帖仍「返信 0 件」，要离页再进；
        投稿在线也一律提示「オンライン後に送信します」。现在 worker 每笔有结局就通知，壳统一提示并让页面重画。"""
        sync = _code("NextSocialSync.cs")
        self.assertIn("public event Action<SocialMutation, Outcome, string> Settled;", sync)
        accepted = sync[sync.index("_api.Social.MarkAccepted("):]
        self.assertIn("Notify(mutation, Outcome.Accepted", accepted[:accepted.index("continue;")])
        failed = sync[sync.index("_api.Social.MarkFailed("):]
        self.assertIn("Outcome.Deferred : Outcome.Rejected", failed[:failed.index("yield break;")])
        self.assertIn("catch (Exception error) { Debug.LogException(error); }", sync)

        shell = _code("NextShell.cs")
        self.assertIn("Api.SocialSync.Settled -= OnSocialSettled;", shell)
        self.assertIn("Api.SocialSync.Settled += OnSocialSettled;", shell)
        handler = shell[shell.index("private void OnSocialSettled("):]
        handler = handler[:handler.index("\n        }\n")]
        self.assertIn('Status(NextStrings.Get("feed_posted"))', handler)
        self.assertIn("mutation.Attempts == 0", handler)          # 离线只提示一次
        self.assertIn("_post?.OnSocialSettled(mutation, outcome);", handler)
        self.assertIn("_feed?.OnSocialSettled(mutation, outcome);", handler)

        compose = _code("NextComposeView.cs")
        self.assertNotIn('"feed_queued"', compose)                 # 入队时不再先下结论

        detail = _code("NextPostView.cs")
        settled = detail[detail.index("public void OnSocialSettled("):]
        self.assertIn("Reload();", settled[:settled.index("\n        }\n")])

    def test_m3_board_is_separate_from_chat_and_reuses_media_upload(self):
        feed = _code("NextFeedView.cs")
        compose = _code("NextComposeView.cs")
        detail = _code("NextPostView.cs")
        shell = _code("NextShell.cs")
        self.assertIn("Api.Get(NextBoardPaths.Feed(PageSize, since)", feed)
        self.assertIn("DateTime.Today", feed)
        self.assertIn("next_cursor", feed)
        self.assertIn("verticalNormalizedPosition = 1f", feed)
        self.assertNotIn("NextLayout.TwoPane", feed)
        self.assertNotIn("ChatComposer", feed)
        self.assertIn("Api.UploadMedia(bytes", compose)
        self.assertIn("Api.Post(NextBoardPaths.Posts, body", compose)
        self.assertNotIn("Api.Turn(", compose)
        self.assertIn("_compose?.AddPickedImage(bytes)", shell)
        self.assertIn("_chat?.SendPickedImage(bytes", shell)
        self.assertIn("NextBoardPaths.Replies(_id)", detail)
        self.assertNotIn("SwitchEntity", detail)
        self.assertNotIn("GuestChat", feed + compose + detail)
        self.assertNotIn("RoomChat", feed + compose + detail)
        # 公共区直连 App Server（2026-11-08，PENDING B.App.8）：路径只在 NextBoardPaths 写一次，
        # 客户端任何地方都不能再打 Agent 的 `/api/feed`／`/api/post` 兼容入口。
        paths = _code("NextBoardPaths.cs")
        self.assertIn('"/v1/feed?limit="', paths)
        self.assertIn('Posts = "/v1/posts"', paths)
        self.assertIn('"/replies"', paths)
        sync = _code("NextSocialSync.cs")
        for name in ("NextBoardPaths.Posts", "NextBoardPaths.Replies(", "NextBoardPaths.Like(",
                     "NextBoardPaths.Post("):
            self.assertIn(name, sync)
        for path in sorted(NEXT.glob("*.cs")):
            if path.name == "NextBoardPaths.cs":
                continue
            code = _code(path.name)
            self.assertNotIn('"/api/feed', code, path.name)
            self.assertNotIn('"/api/post', code, path.name)
        api = _code("NextApi.cs")
        delete = api[api.index("public void Delete("):]
        self.assertIn("UnityWebRequest.kHttpVerbDELETE", delete[:delete.index("\n        }\n")])
        self.assertIn("RefreshTopUnread();", shell)   # 未讀數改在頂欄（設計稿 v4 ⑦）

    def test_minor_logout_keeps_device_preferences(self):
        config = _code("NextConfig.cs")
        clear = config[config.index("public static void ClearToken()"):]
        clear = clear[:clear.index("\n        }\n")]
        self.assertFalse("KeyUiLanguage" in clear, "換身份不該把界面語言重設")
        self.assertFalse("KeyVolume" in clear)
        self.assertTrue(re.search(r"KeyToken", clear))

    def test_logout_clears_indexed_identity_caches_and_legacy_lumi(self):
        config = _code("NextConfig.cs")
        clear = config[config.index("public static void ClearToken()"):]
        clear = clear[:clear.index("\n        }\n")]
        self.assertIn("KeyIdentityKeys", clear)
        self.assertIn("PlayerPrefs.DeleteKey(key)", clear)
        self.assertIn('"lumi"', clear)
        self.assertIn('KeyLastTurn + "_" + entity', clear)
        self.assertIn('"ba_next_mood_" + entity', clear, "舊版私有心情殘留仍要清")
        self.assertIn("KeyLiveTurn + entity", clear)
        self.assertEqual(config.count("TrackIdentityKey(key);"), 2,
                         "LastTurn／LiveTurn 寫入時都要登記；平台心情已改存 SQLite")


class TestMoodEventRouting(unittest.TestCase):
    def test_transport_preserves_event_name_and_shell_separates_mood_from_chat(self):
        api = _code("NextApi.cs")
        self.assertIn("Action<string, JObject> onEvent", api)
        # 逐幀派送改走共用層 `SseFramePump`（2026-11-04）：事件名照樣原樣往上交，
        # 但游標只在處理成功後才推進（以前是先 Advance 再 onEvent ⇒ 一幀炸掉整批丟）。
        self.assertIn("onEvent(frame.Event, json)", api)
        self.assertIn("SseFramePump.Pump(", api)
        self.assertIn("id => Cursors.Advance(id)", api)
        self.assertIn("ref cursorBlocked", api, "失敗缺口要跨同一連線的各批次保留")
        pump = api[api.index("SseFramePump.Pump("):]
        self.assertLess(pump.index("onEvent(frame.Event, json)"), pump.index("id => Cursors.Advance(id)"),
                        "游標必須在 onEvent **之後**才推進（否則失敗的幀永遠補不回來）")
        shell = _code("NextShell.cs")
        route = shell[shell.index("private void OnEvent("):]
        route = route[:route.index("\n        }\n")]
        self.assertIn('eventName == "mood.updated"', route)
        self.assertIn("Api.Social?.ApplyEvent(payload)", route)
        self.assertIn("NextMoodBadge.OnEvent(payload)", route)
        self.assertIn('eventName == "chat.message.created"', route)
        self.assertIn("Api.Chat?.ApplyEvent(payload, ChatAccountId)", route)   # 群未讀在落庫時累加
        self.assertNotIn("_chat.OnDelivery", route)
        resume = shell[shell.index("public void ResumeSync()"):]
        resume = resume[:resume.index("\n        }\n")]
        self.assertIn("NextMoodBadge.Sync()", resume, "重連後用公開心情快照對賬")

    def test_badge_filters_account_and_uses_public_sqlite_snapshot(self):
        badge = _code("NextMoodBadge.cs")
        self.assertIn('OptJObject("payload")?.OptJObject("mood")', badge)
        self.assertIn('mood.OptString("account_id") != badge._entity', badge)
        self.assertIn("_shell.Api.Social?.Mood(_entity)", badge)
        self.assertNotIn("NextConfig.SetMoodFor", badge)
        self.assertNotIn("valence", badge)
        self.assertNotIn("social_need", badge)
        self.assertIn("private void PaintCached()", badge)
        self.assertIn("version != _eventVersion", badge, "舊 GET 回包不能蓋掉新推送")
        self.assertIn("private const float RefreshSeconds = 120f", badge)
        shell = _code("NextShell.cs")
        resume = shell[shell.index("public void ResumeSync()"):]
        resume = resume[:resume.index("\n        }\n")]
        self.assertIn("NextMoodBadge.Sync()", resume)
        chat = _code("NextChatView.cs")
        self.assertNotIn("NextMoodBadge.Poke()", chat, "回合結束改由伺服器事件通知，不再額外輪詢")



class TestDesignAlignment20261107(unittest.TestCase):
    """UI 对齐设计稿（2026-11-07 review）：改回去就红。"""

    def test_feed_card_has_no_delete_and_light_counts(self):
        feed = _code("NextFeedView.cs")
        board = _code("NextBoardUi.cs")              # 2026-11-08 起卡片在 PostCard（公共区与主页共用）
        card = board[board.index("public static RectTransform PostCard("):]
        card = card[:card.index("\n        }\n")]
        self.assertNotIn("DeleteButton", card)
        self.assertIn("compact: true", card)
        self.assertIn("Folded(body, post.OptBoolean(\"truncated\"))", card)
        self.assertIn("<noparse>", board)

    def test_feed_has_no_header_row_and_pulls_to_refresh(self):
        """owner 2026-11-08：公共区拿掉「標題＋重新整理」那一排，改下拉；往上滑收起投稿數。"""
        feed = _code("NextFeedView.cs")
        self.assertNotIn('"FeedHead"', feed)
        self.assertNotIn('"FeedRefresh"', feed)
        self.assertIn("AddComponent<NextPullRefresh>()", feed)
        self.assertIn("ScrollRect.MovementType.Elastic", feed)
        self.assertIn("pull.OnRefresh = Reload", feed)
        scrolled = feed[feed.index("private void OnScrolled()"):]
        self.assertIn("ShowToday(false)", scrolled[:scrolled.index("\n        }\n")])

    def test_delete_needs_second_tap(self):
        board = _code("NextBoardUi.cs")
        delete = board[board.index("public static Button DeleteButton("):]
        self.assertIn("feed_delete_confirm", delete[:delete.index("Api.Social.EnqueueDelete")])

    def test_people_do_not_get_a_character_face(self):
        board = _code("NextBoardUi.cs")
        heading = board[board.index("public static RectTransform Heading("):]
        heading = heading[:heading.index("\n        }\n")]
        self.assertIn("PersonAvatar(", heading)

    def test_chat_header_is_above_the_stage(self):
        chat = _code("NextChatView.cs")
        self.assertLess(chat.index('UiBuilder.Row("ChatHead"'), chat.index("NextStageView.Build("))

    def test_chat_stage_uses_the_planned_height(self):
        """1:1 舞台 224（PLAN §4.2）；通話頁仍是預設的 320。"""
        self.assertIn("public const float ChatStageHeight = 224f;", _code("NextStageView.cs"))
        self.assertIn("NextStageView.ChatStageHeight)", _code("NextChatView.cs"))
        self.assertNotIn("ChatStageHeight", _code("NextCallView.cs"))

    def test_group_author_is_a_label_above_the_bubble(self):
        chat = _code("NextChatView.cs")
        bubble = chat[chat.index("private RectTransform BuildBubble("):]
        bubble = bubble[:bubble.index("\n        }\n")]
        self.assertIn('"BubbleAuthor"', bubble)

    def test_previews_are_localised(self):
        for name in ("NextChatListView.cs", "NextChatView.cs"):
            code = _code(name)
            self.assertNotIn('"[sticker]"', code)
            self.assertNotIn('"[photo]"', code)

if __name__ == "__main__":
    unittest.main(verbosity=2)
