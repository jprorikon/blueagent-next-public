#!/usr/bin/env python3
"""Discord 源（移管过来的现役 bot）：谁说的算谁的、该不该回、回什么。

判据：
  ① 身分按 **Discord id** 映射：`ALLOWED_USER_IDS` = 主人（`owner`），其他人 `guest-<id>`；
     昵称改不掉身分；
  ② 该不该回：DM／被 @／叫了名字／指定频道 ⇒ 回；其他群消息 ⇒ `observed`（旁听不插话）；
  ③ 叫名字换实体：`ミラ/アスター/ノリ`；
  ④ **权限不因来源放宽**：guest 在 Discord 说「帮我删掉」，仍是 chat，不触发委任；
  ⑤ 回话带音频（SSE 的 speech 帧解成 wav 字节）；
  ⑥ 没 token ⇒ 不建源（不連線、不佔資源）。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.discord_source import (DiscordConfig, DiscordRouter,
                                                    DiscordSource, build_discord)

OWNER_ID = "100000000000000001"
GUEST_ID = "777000111222333444"


def config(**overrides) -> DiscordConfig:
    values = {"DISCORD_BOT_TOKEN": "test-token", "GUILD_ID": "g1",
              "TEXT_CHANNEL_ID": "c1", "ALLOWED_USER_IDS": OWNER_ID}
    values.update(overrides)
    return DiscordConfig.from_env(values)


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.router = DiscordRouter(config())

    def test_owner_by_id(self):
        self.assertEqual(self.router.principal_for(OWNER_ID), "owner")
        self.assertTrue(self.router.is_owner(OWNER_ID))

    def test_unknown_id_is_nobody(self):
        """白名單外＝不認識（不是「自動變 guest」）——owner 2026-10-22 的缺陷就在這裡。"""
        self.assertEqual(self.router.principal_for(GUEST_ID), "")
        self.assertFalse(self.router.is_owner(GUEST_ID))

    def test_nickname_cannot_change_identity(self):
        self.assertEqual(self.router.principal_for("123"), "")
        self.assertEqual(self.router.principal_for(OWNER_ID), "owner")

    def test_entity_is_always_the_bot_identity(self):
        """一顆 bot ↔ 一個實體：喊別的角色名也不會換人回（owner 2026-10-22）。"""
        self.assertEqual(self.router.entity_for("ミラ、今日どう？"), "mira")
        self.assertEqual(self.router.entity_for("ノリは元気？"), "mira")
        self.assertEqual(self.router.entity_for("アスターおはよう"), "mira")
        self.assertEqual(self.router.entity_for("ただの雑談"), "mira")

    def test_other_character_names_are_not_this_bot(self):
        """「ノリ」不算在叫她 ⇒ 群裡只是旁聽，不會被叫醒。"""
        self.assertEqual(self.router.addressed_hint("ノリ、今日の観測は？", mentioned=False,
                                                     is_dm=False), "ambient")
        self.assertEqual(self.router.addressed_hint("ミラ、聞こえる？", mentioned=False,
                                                     is_dm=False), "named")

    def test_addressed_hint_is_input_not_answer(self):
        """這裡只提供「有沒有被直接叫到」的事實；回不回由注意力層判（owner 2026-10-22）。"""
        self.assertEqual(self.router.addressed_hint("hi", mentioned=True, is_dm=False), "direct")
        self.assertEqual(self.router.addressed_hint("hi", mentioned=False, is_dm=True), "direct")
        self.assertEqual(self.router.addressed_hint("ミラ、聞こえる？", mentioned=False,
                                                     is_dm=False), "named")
        self.assertEqual(self.router.addressed_hint("今日はいい天気", mentioned=False,
                                                    is_dm=False), "ambient")

    def test_mention_is_stripped(self):
        self.assertEqual(self.router.clean_text(f"<@{OWNER_ID}> こんばんは"), "こんばんは")


class TestSourceHandle(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.source = DiscordSource(config(), self.state)

    def test_owner_message_gets_a_reply(self):
        result = self.source.handle("こんばんは", OWNER_ID, guild=False, mentioned=False,
                                    is_dm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["principal"], "owner")
        self.assertTrue(result["text"])

    def _approve_guest(self) -> str:
        """走完 invite→approve，讓 GUEST_ID 真的變成 guest（之後她才該理他）。"""
        joined = self.state.join_guest(invite_code="blueagent", discord_id=GUEST_ID,
                                       display_name="客人", declared_kind="friend")
        approved = self.state.approve_guest(joined["request_id"], final_kind="friend")
        return approved["request"]["principal_id"]

    def test_stranger_is_ignored_entirely(self):
        """白名單外：不回、也不 ingest（owner 2026-10-22：「白名單以外都不回」）。"""
        before = len(self.state.route_log)
        result = self.source.handle("<@ミラ> 在么", GUEST_ID, guild=True,
                                    mentioned=True, is_dm=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_whitelisted")
        self.assertEqual(len(self.state.route_log), before, "連路由都不該進")

    def test_approved_guest_gets_a_reply_but_stays_chat(self):
        principal = self._approve_guest()
        result = self.source.handle("帮我删掉那个文件夹", GUEST_ID, guild=False,
                                    mentioned=False, is_dm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["principal"], principal)
        decisions = [frame for frame in self.state.route_log
                     if str(frame.get("principal")) == principal]
        self.assertTrue(decisions, "这一轮应该进过路由")
        self.assertEqual(decisions[-1]["entity"], "mira")
        # 權限不因來源放寬：還是 chat，不委任
        self.assertEqual(result.get("mode") or "chat", "chat")

    def test_unaddressed_group_message_from_guest_is_observed(self):
        self._approve_guest()
        result = self.source.handle("今日はいい天気だね", GUEST_ID, guild=True,
                                    mentioned=False, is_dm=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "observed")

    def test_owner_in_group_without_mention_is_also_observed(self):
        """**多人頻道不因說話的人是主人就回**——這正是注意力原則（owner 2026-10-22 糾正）。"""
        result = self.source.handle("ただの独り言です", OWNER_ID, guild=True,
                                    mentioned=False, is_dm=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "observed")

    def test_owner_in_group_when_addressed_gets_a_reply(self):
        result = self.source.handle(f"<@{OWNER_ID}> ミラ、聞こえる？", OWNER_ID,
                                    guild=True, mentioned=True, is_dm=False)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["text"])

    def test_naming_another_character_does_not_switch_entity(self):
        """喊「ノリ」：這顆 bot 是 mira ⇒ 群裡只是旁聽，且實體不會被換掉。"""
        result = self.source.handle("ノリ、今日の観測は？", OWNER_ID, guild=True,
                                    mentioned=False, is_dm=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "observed")

    def test_audio_is_decoded_when_present(self):
        self.assertEqual(DiscordSource._decode_audio("UklGRg=="), b"RIFF")
        self.assertEqual(DiscordSource._decode_audio(""), b"")

    def test_reply_text_is_not_empty(self):
        """回話文字要真的取到（外層 SSE 幀 / 內層 payload 兩層；取錯一層會回「（她沒有回話）」）。"""
        result = self.source.handle("こんばんは", OWNER_ID, guild=False, mentioned=False,
                                    is_dm=True)
        self.assertTrue(result["text"])
        self.assertNotIn("她沒有回話", result["text"])

    def test_empty_message_is_ignored(self):
        self.assertEqual(self.source.handle("   ", OWNER_ID, guild=False, mentioned=False,
                                            is_dm=True)["reason"], "empty")


class TestDmStaysInDiscord(unittest.TestCase):
    """私訊的履歷不回流到 App（owner 2026-10-29：上下文共用，但各管線的履歷只出現在各自管線）。

    以前私訊不標來源 ⇒ `routing_for` 對非群聊一律預設 `phone:<principal>` ⇒ App 的對話紀錄
    （`/api/turns?source=phone,api`）把私訊也拉回來。
    """

    def setUp(self):
        from agent.brain.domain.auth import AuthContext, Role
        from agent.server import api as api_module
        self.api = api_module
        self.owner = AuthContext(principal="owner", role=Role.OWNER, capabilities=frozenset({"chat"}))
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.source = DiscordSource(config(), self.state)

    def app_history(self):
        _, data = self.api.list_turns(self.state, {"entity": ["mira"], "source": ["phone,api"]},
                                      auth=self.owner)
        return [row["asked"] for row in data["turns"]]

    def test_dm_is_recorded_as_discord_and_not_shown_in_app(self):
        self.source.handle("DMのテスト", OWNER_ID, guild=False, mentioned=False, is_dm=True)
        rows = self.state.app.outbox.since("", principal="owner", limit=10)
        self.assertEqual([row["source"] for row in rows], ["discord:dm:owner"])
        self.assertEqual(self.app_history(), [])

    def test_app_turn_still_shows_and_shares_the_thread_with_the_dm(self):
        self.source.handle("DMのテスト", OWNER_ID, guild=False, mentioned=False, is_dm=True)
        list(self.state.send_stream(text="アプリから", entity="mira", principal="owner"))
        self.assertEqual(self.app_history(), ["アプリから"])
        # 上下文共用：兩邊落在同一條線（她在 App 裡記得私訊說過什麼）
        threads = list(self.state.app.threads.threads())
        self.assertEqual(len(threads), 1)
        said = [row.text for row in threads[0].transcript if row.role == "user"]
        self.assertEqual(said, ["DMのテスト", "アプリから"])

    def test_dm_does_not_get_the_app_only_sticker_tool(self):
        from agent.brain.runtime.tools.sticker import sticker_visible
        self.assertFalse(sticker_visible("discord:dm:owner"))


class TestWiring(unittest.TestCase):
    def test_no_token_means_no_source(self):
        self.assertIsNone(build_discord(None, env={}))
        self.assertIsNone(build_discord(None, env={"DISCORD_BOT_TOKEN": ""}))

    def test_token_creates_source_without_connecting(self):
        source = build_discord(None, env={"DISCORD_BOT_TOKEN": "t",
                                          "ALLOWED_USER_IDS": OWNER_ID})
        self.assertIsNotNone(source)
        self.assertTrue(source.config.enabled)
        self.assertFalse(source.health()["ok"], "还没 start 就不该说已连上")

    def test_threads_are_ignored_by_default(self):
        cfg = DiscordConfig.from_env({"DISCORD_BOT_TOKEN": "t"})
        self.assertTrue(cfg.ignore_threads, "預設忽略 thread，避免兩隻 bot 在同一串重複回")
        off = DiscordConfig.from_env({"DISCORD_BOT_TOKEN": "t",
                                      "BLUEAGENT_DISCORD_IGNORE_THREADS": "0"})
        self.assertFalse(off.ignore_threads)

    def test_env_file_keys_are_read(self):
        cfg = DiscordConfig.from_env({"DISCORD_BOT_TOKEN": "t", "GUILD_ID": "1",
                                      "TEXT_CHANNEL_ID": "2", "VOICE_CHANNEL_ID": "3",
                                      "ALLOWED_USER_IDS": f"{OWNER_ID}, 42",
                                      "BLUEAGENT_DISCORD_AUDIO": "0"})
        self.assertEqual(cfg.guild_id, "1")
        self.assertEqual(cfg.channel_id, "2")
        self.assertEqual(cfg.voice_channel_id, "3")
        self.assertEqual(cfg.owner_ids, frozenset({OWNER_ID, "42"}))
        self.assertFalse(cfg.reply_with_audio)


if __name__ == "__main__":
    unittest.main(verbosity=2)
