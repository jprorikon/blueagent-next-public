#!/usr/bin/env python3
"""多顆 Discord bot（owner 2026-10-27「做 1 到 4」）：一顆 bot ↔ 一個實體。

判據：
  ① 非主 bot 的設定從 `BLUEAGENT_DISCORD_<ENTITY>_*` 去前綴後得到（可直接餵 DiscordConfig）；
  ② aster 的 bot 名字表只有 aster（喊「ミラ」不該叫醒 aster）；
  ③ 主動訊息／語音按 frame 的 entity 挑**那個角色自己的** bot；
  ④ 主源仍是 mira（舊呼叫端 `app.discord` 不受影響）。
"""

from __future__ import annotations

import unittest

from agent.adapter.discord_source.bots import bots_from_env, sources_by_entity
from agent.adapter.discord_source.config import DiscordConfig


ENV = {
    "DISCORD_BOT_TOKEN": "main.token.value",
    "GUILD_ID": "g-main",
    "TEXT_CHANNEL_ID": "c-main",
    "ALLOWED_USER_IDS": "1,2",
    "BLUEAGENT_DISCORD_ASTER_TOKEN": "aster.token.value",
    "BLUEAGENT_DISCORD_ASTER_GUILD_ID": "g-main",
    "BLUEAGENT_DISCORD_ASTER_TEXT_CHANNEL_ID": "c-aster",
    "BLUEAGENT_DISCORD_ASTER_VOICE_CHANNEL_ID": "v-aster",
    "BLUEAGENT_DISCORD_ASTER_ALLOWED_USER_IDS": "1",
    "BLUEAGENT_DISCORD_ASTER_ENTITY": "aster",
    "BLUEAGENT_DISCORD_ASTER_VC": "1",
}


class TestBotsFromEnv(unittest.TestCase):
    def test_extra_bot_is_unprefixed_and_marked(self):
        table = bots_from_env(ENV)
        self.assertEqual(sorted(table), ["aster"])
        mapped = table["aster"]
        self.assertEqual(mapped["DISCORD_BOT_TOKEN"], "aster.token.value")
        self.assertEqual(mapped["TEXT_CHANNEL_ID"], "c-aster")
        self.assertEqual(mapped["VOICE_CHANNEL_ID"], "v-aster")
        self.assertEqual(mapped["DEFAULT_ENTITY"], "aster")
        self.assertEqual(mapped["BLUEAGENT_DISCORD_VC"], "1")

    def test_extra_bot_inherits_owner_whitelist(self):
        """★ 沒給 `..._ASTER_ALLOWED_USER_IDS` ⇒ 沿用主 bot 的主人名單（以前是空的，主人被當名單外）。"""
        env = {key: value for key, value in ENV.items()
               if key != "BLUEAGENT_DISCORD_ASTER_ALLOWED_USER_IDS"}
        config = DiscordConfig.from_env(bots_from_env(env)["aster"])
        self.assertEqual(config.owner_ids, frozenset({"1", "2"}))

    def test_extra_bot_own_whitelist_wins(self):
        config = DiscordConfig.from_env(bots_from_env(ENV)["aster"])
        self.assertEqual(config.owner_ids, frozenset({"1"}))

    def test_main_bot_is_not_treated_as_extra(self):
        self.assertEqual(bots_from_env({"DISCORD_BOT_TOKEN": "x"}), {})
        # `..._ENTITY` 這類設定鍵不是一顆 bot
        self.assertEqual(bots_from_env({"BLUEAGENT_DISCORD_ENTITY": "aster"}), {})

    def test_two_extra_bots_are_both_parsed(self):
        """aster 與 nori 各自一顆（owner 2026-10-27 同日交了兩把 token）。"""
        env = dict(ENV)
        env.update({"BLUEAGENT_DISCORD_NORI_TOKEN": "nori.token.value",
                    "BLUEAGENT_DISCORD_NORI_TEXT_CHANNEL_ID": "c-nori",
                    "BLUEAGENT_DISCORD_NORI_ENTITY": "nori"})
        table = bots_from_env(env)
        self.assertEqual(sorted(table), ["aster", "nori"])
        nori = DiscordConfig.from_env(table["nori"])
        self.assertEqual((nori.default_entity, nori.channel_id), ("nori", "c-nori"))
        self.assertEqual(set(nori.names.values()), {"nori"})

    def test_config_from_mapped_env_is_aster_only(self):
        config = DiscordConfig.from_env(bots_from_env(ENV)["aster"])
        self.assertEqual(config.default_entity, "aster")
        self.assertEqual(config.channel_id, "c-aster")
        self.assertEqual(set(config.names.values()), {"aster"}, "名字表只能有 aster")
        self.assertIn("アスター", config.names)

    def test_main_config_stays_mira(self):
        config = DiscordConfig.from_env(ENV)
        self.assertEqual(config.default_entity, "mira")
        self.assertEqual(set(config.names.values()), {"mira"})


class FakeSource:
    def __init__(self, entity, channel):
        self.config = type("C", (), {"default_entity": entity, "channel_id": channel})()
        self.sent = []

    def send_text(self, text):
        self.sent.append(text)
        return True


class TestSourcesByEntity(unittest.TestCase):
    def test_table_keyed_by_entity(self):
        table = sources_by_entity([FakeSource("mira", "c1"), FakeSource("aster", "c2")])
        self.assertEqual(sorted(table), ["aster", "mira"])


class TestDeliveryRouting(unittest.TestCase):
    def test_channel_picks_bot_by_frame_entity(self):
        from agent.brain.runtime.delivery.delivery import DiscordChannel

        main, aster = FakeSource("mira", "c-main"), FakeSource("aster", "c-aster")
        channel = DiscordChannel(main)
        channel.sources = {"mira": main, "aster": aster}
        self.assertIs(channel.source_for({}), main)
        self.assertIs(channel.source_for({"entity": "aster"}), aster)
        channel.send("owner", {"text": "アスターだよ", "entity": "aster"})
        self.assertEqual(aster.sent, ["アスターだよ"])
        self.assertEqual(main.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

