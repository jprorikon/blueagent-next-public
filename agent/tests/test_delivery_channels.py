#!/usr/bin/env python3
"""主動發言走哪條信道（owner 2026-10-22）。

判據：
  ① 他剛在哪裡說話 ⇒ 回到那裡（15 分鐘內）。
  ② 否則 App → Discord DM → 群（群只放 public）。
  ③ **私事不進群**。
  ④ 一條都不通 ⇒ 這次不說（不是亂喊）。
  ⑤ 送失敗降級一次，不重試。
"""

from __future__ import annotations

import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.delivery.bus import EventBus
from agent.brain.runtime.delivery.delivery import AppChannel, Channel, ChannelRouter, DiscordChannel


class FakeChannel(Channel):
    def __init__(self, name: str, *, up: bool = True, allows_private: bool = True,
                 works: bool = True) -> None:
        self.name = name
        self.allows_private = allows_private
        self.up = up
        self.works = works
        self.sent: list[dict] = []

    def available(self, principal: str) -> bool:
        return self.up

    def send(self, principal: str, frame: dict) -> bool:
        if not self.works:
            return False
        self.sent.append(frame)
        return True


class FakeDiscord:
    class config:
        channel_id = "chan-1"

    def __init__(self, *, dm_ok: bool = True, known: bool = True) -> None:
        self._client = object()
        self.dm_ok = dm_ok
        self.known = known
        self.dms: list[str] = []
        self.posts: list[str] = []

    def can_dm(self, principal: str) -> bool:
        return self.known

    def send_dm(self, principal: str, text: str) -> bool:
        if not self.dm_ok:
            return False
        self.dms.append(text)
        return True

    def send_text(self, text: str) -> bool:
        self.posts.append(text)
        return True


class TestChannelSelection(unittest.TestCase):
    def test_goes_back_to_where_he_just_spoke(self):
        app = FakeChannel("app")
        discord = FakeChannel("discord")
        router = ChannelRouter([app, discord])
        router.note_seen("owner", "discord")
        result = router.send("owner", {"text": "ねえ", "visibility": "public"})
        self.assertEqual(result["channel"], "discord", result)
        self.assertEqual(discord.sent[0]["text"], "ねえ")
        self.assertEqual(app.sent, [])

    def test_default_order_prefers_app(self):
        app = FakeChannel("app")
        discord = FakeChannel("discord")
        router = ChannelRouter([app, discord])
        result = router.send("owner", {"text": "ねえ", "visibility": "public"})
        self.assertEqual(result["channel"], "app")

    def test_private_never_goes_to_a_group_channel(self):
        group = FakeChannel("group", allows_private=False)
        app = FakeChannel("app", up=False)
        router = ChannelRouter([app, group])
        result = router.send("owner", {"text": "ちょっと落ち込んでる", "visibility": "private"})
        self.assertFalse(result["sent"])
        self.assertEqual(group.sent, [], "私事不能進群")

    def test_falls_through_when_a_channel_fails(self):
        broken = FakeChannel("app", works=False)
        back = FakeChannel("discord")
        router = ChannelRouter([broken, back])
        result = router.send("owner", {"text": "ねえ"})
        self.assertEqual(result["channel"], "discord", result)
        self.assertEqual(router.stats["failed"], 1)

    def test_stale_recent_channel_expires(self):
        router = ChannelRouter([FakeChannel("app"), FakeChannel("discord")], recent_s=60)
        router.note_seen("owner", "discord", at=time.time() - 600)
        self.assertEqual(router.last_channel("owner"), "")
        self.assertEqual(router.send("owner", {"text": "ねえ"})["channel"], "app")


class TestDiscordChannel(unittest.TestCase):
    def test_public_goes_to_group_first(self):
        """owner 2026-10-22 改的規矩：公開的事先在群裡說，不要戳私聊。

        舊行為（先 DM）＝每次重啟她都會私聊一句，看起來像推銷。
        """
        source = FakeDiscord(dm_ok=True)
        channel = DiscordChannel(source)
        self.assertTrue(channel.available("owner"))
        self.assertTrue(channel.send("owner", {"text": "ねえ"}))
        self.assertEqual(source.posts, ["ねえ"], "公開的先貼群")
        self.assertEqual(source.dms, [], "群貼得上就不 DM")

    def test_private_skips_group_when_dm_fails(self):
        source = FakeDiscord(dm_ok=False)
        channel = DiscordChannel(source)
        self.assertFalse(channel.send("owner", {"text": "落ち込んでる", "visibility": "private"}))
        self.assertEqual(source.posts, [])

    def test_public_falls_back_to_group(self):
        source = FakeDiscord(dm_ok=False)
        channel = DiscordChannel(source)
        self.assertTrue(channel.send("owner", {"text": "散歩してきた", "visibility": "public"}))
        self.assertEqual(source.posts, ["散歩してきた"])


class TestLonelyGoesToVoice(unittest.TestCase):
    def test_lonely_prefers_voice_when_someone_is_in_the_channel(self):
        from agent.brain.runtime.delivery.delivery import DiscordVoiceChannel

        class FakeVoice:
            joined = True
            def __init__(self):
                self.said: list[str] = []

            def humans_present(self):
                return True

            def say(self, text):
                self.said.append(text)
                return True

        voice = FakeVoice()
        source = type("S", (), {"voice": voice, "config": type("C", (), {"channel_id": ""})()})()
        router = ChannelRouter([FakeChannel("app"), DiscordVoiceChannel(source)])
        result = router.send("owner", {"text": "ねえ、声が聞きたい", "urgency": "lonely"})
        self.assertEqual(result["channel"], "voice", result)
        self.assertEqual(voice.said, ["ねえ、声が聞きたい"])

    def test_lonely_falls_back_when_nobody_is_there(self):
        from agent.brain.runtime.delivery.delivery import DiscordVoiceChannel

        class FakeVoice:
            joined = True
            def humans_present(self):
                return False

            def say(self, text):                     # pragma: no cover - 不該被叫到
                raise AssertionError("沒人在就不該在空頻道說話")

        posted: list[str] = []

        class FakeSource:
            voice = FakeVoice()
            config = type("C", (), {"channel_id": "chan-1"})()

            def can_dm(self, principal):
                return False

            def send_text(self, text):
                posted.append(text)
                return True

        router = ChannelRouter([FakeChannel("app"), DiscordVoiceChannel(FakeSource())])
        result = router.send("owner", {"text": "ちょっと寂しい", "urgency": "lonely"})
        # App 先接住了；語音沒人就不硬說
        self.assertTrue(result["sent"], result)
        self.assertEqual(posted, [])


class TestAppChannelUsesBus(unittest.TestCase):
    def test_no_subscriber_means_not_available(self):
        bus = EventBus()
        channel = AppChannel(bus)
        self.assertFalse(channel.available("owner"), "沒開 App 就不算送得到")
        sub_id, _queue = bus.subscribe("owner")
        try:
            self.assertTrue(channel.available("owner"))
        finally:
            bus.unsubscribe("owner", sub_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
