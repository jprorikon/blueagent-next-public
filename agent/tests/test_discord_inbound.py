#!/usr/bin/env python3
"""Discord 進站→回話（`on_message`）的回歸：DM 進來必須真的走到 `handle` 並回一句。

為什麼要有這支（2026-10-24 實測踩到的坑）：
`on_message` 的觀測日誌裡引用了一個只存在於 `handle()` 內的局部變數 `condition`
⇒ 每一則進站訊息在寫日誌那一刻就 `NameError`（discord.py 只把它記成
`Ignoring exception in on_message`，bot 照樣在線、心跳照樣發、回話全無）。
症狀是他「私信回她，她一聲不吭」，而 attention/turns 兩份日誌完全沒有新紀錄。

判據：
  ① 私信（DM）一則 ⇒ 走到 `handle`、回一句（不是例外、不是靜默）
  ② 頻道裡 @ 她 ⇒ 一樣回
  ③ 頻道裡沒 @、她也剛沒說話 ⇒ 這則不該回（旁聽），但**日誌要寫得出來**（同樣不能拋例外）
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))   # 倉庫根

from agent.adapter.discord_source import DiscordConfig, DiscordSource  # noqa: E402

OWNER_ID = "100000000000000001"
BOT_ID = "100000000000000002"


# ── 假 discord 模組（只要 `_make_client` 會用到的幾個名字）─────────────────────
class FakeUser:
    def __init__(self, user_id: str, name: str = "owner", bot: bool = False) -> None:
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = bot

    def __str__(self) -> str:
        return self.name


class FakeThread:
    pass


class FakeClient:
    def __init__(self, *, intents=None) -> None:
        self.intents = intents
        self.user = None
        self.handlers: dict = {}

    def event(self, func):                      # discord.py 的裝飾器：只是登記
        self.handlers[func.__name__] = func
        return func


class FakeIntents:
    def __init__(self) -> None:
        self.message_content = False
        self.guilds = False

    @classmethod
    def default(cls):
        return cls()


class FakeDiscord:
    Intents = FakeIntents
    Client = FakeClient
    Thread = FakeThread
    PrivilegedIntentsRequired = type("PrivilegedIntentsRequired", (Exception,), {})

    class File:
        def __init__(self, fp=None, filename: str = "") -> None:
            self.fp = fp
            self.filename = filename


# ── 假訊息 / 假 state ─────────────────────────────────────────────────────────
class FakeChannel:
    def __init__(self, channel_id: str) -> None:
        self.id = channel_id
        self.sent: list[dict] = []

    async def fetch_message(self, message_id):        # `_is_reply_to_me` 的退路
        return None

    async def send(self, content: str = "", **kwargs) -> None:
        self.sent.append({"content": content, **kwargs})


class FakeMessage:
    def __init__(self, text: str, *, guild, mentions=()) -> None:
        self.content = text
        self.author = FakeUser(OWNER_ID)
        self.guild = guild
        self.channel = FakeChannel("555000111")
        self.mentions = list(mentions)
        self.reference = None
        self.referenced_message = None
        self.replied: list[str] = []

    async def reply(self, content: str = "", **kwargs) -> None:
        self.replied.append(content)


class FakeState:
    """只回一句 `done` 幀；足以證明「有走到 handle 並且拿到回話」。"""

    def __init__(self, *, observed: bool = False, audio: str = "") -> None:
        self.observed = observed
        self.audio = audio
        self.calls: list[dict] = []

    def send_stream(self, **kwargs):
        self.calls.append(kwargs)
        yield ("result", {"observed": self.observed,
                          "decision": {"engage": "OBSERVE" if self.observed else "REPLY"}})
        if self.observed:
            return
        # 真實順序：文字（done）先到，語音（speech）後到
        yield ("frame", {"kind": "done", "payload": {"text": "うん、聞こえてるよ"}})
        if self.audio:
            yield ("frame", {"kind": "speech",
                             "payload": {"text": "うん、聞こえてるよ", "audio": self.audio}})

    def reply_text(self, result) -> str:
        return str((result or {}).get("text") or "")


def build_source(state) -> DiscordSource:
    config = DiscordConfig(token="fake.token.value", guild_id="1", channel_id="2",
                           owner_ids=frozenset({OWNER_ID}), reply_with_audio=False)
    return DiscordSource(config, state, log=lambda _message: None)


def drive(source, message) -> None:
    client = source._make_client(FakeDiscord, message_content=True)
    client.user = FakeUser(BOT_ID, name="mira", bot=True)
    asyncio.run(client.handlers["on_message"](message))


def _tiny_wav_b64() -> str:
    """一段真的 RIFF wav（`merge_wavs` 只認 RIFF；假字串會被丟掉）。"""
    import base64
    import io
    import wave
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 400)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class TestDiscordTextFirst(unittest.TestCase):
    """文字先發（owner 2026-10-24）：模型吐完先貼字，聲音後面補一則。"""

    def test_text_then_audio_without_duplicating_text(self):
        state = FakeState(audio=_tiny_wav_b64())
        source = build_source(state)
        source.config.reply_with_audio = True          # 有聲音才驗得到「只補語音」那則
        message = FakeMessage("ねえ", guild=None)
        drive(source, message)
        self.assertEqual(message.replied, ["うん、聞こえてるよ"], "文字要先貼出去")
        sent = message.channel.sent
        self.assertEqual(len(sent), 1, f"應該只補一則語音：{sent}")
        self.assertEqual(sent[0].get("content", ""), "", "語音那則不該重複文字")
        self.assertIsNotNone(sent[0].get("file"))

    def test_text_first_can_be_disabled(self):
        state = FakeState(audio=_tiny_wav_b64())
        source = build_source(state)
        source.config.reply_with_audio = True
        source.config.text_first = False
        message = FakeMessage("ねえ", guild=None)
        drive(source, message)
        self.assertEqual(len(message.replied), 1, "關掉時回到「文字+聲音同一則」")


class TestGroupAddressee(unittest.TestCase):
    """群聊不搶話（owner 2026-10-27）：他跟別人／另一個 bot 講話時，她只旁聽。"""

    def test_owner_talking_to_another_bot_is_not_addressed(self):
        state = FakeState()
        source = build_source(state)
        first = FakeMessage("ミラ、いる？", guild=object(),
                            mentions=[FakeUser(BOT_ID, name="mira", bot=True)])
        drive(source, first)
        self.assertEqual(state.calls[-1]["addressed_to"], "entity", "叫她還是要回")
        # 另一個 bot（Hermes）在頻道回話：只做簿記（解除延續），不回
        hermes = FakeMessage("はい、なんでしょう", guild=object())
        hermes.author = FakeUser("9990001", name="Hermes", bot=True)
        drive(source, hermes)
        self.assertEqual(hermes.replied, [])
        # 主人接著跟 Hermes 講（沒 @ 她、沒叫名字）⇒ 不算對她說
        third = FakeMessage("明日の件だけど、10時に頼む", guild=object())
        drive(source, third)
        self.assertEqual(state.calls[-1]["addressed_to"], "unknown",
                         "主人跟另一個 bot 一來一回，她以前會插話")

    def test_owner_talking_to_someone_else_is_not_addressed(self):
        state = FakeState()
        source = build_source(state)
        message = FakeMessage("ノリ、明日の件どうする？", guild=object(),
                              mentions=[FakeUser("7770001", name="ノリ")])
        drive(source, message)
        self.assertEqual(state.calls[-1]["addressed_to"], "unknown")


class TestDiscordInbound(unittest.TestCase):
    def test_dm_reaches_handle_and_replies(self):
        """① 私信：必須回一句（舊碼在這裡 NameError，什麼都不回）。"""
        state = FakeState()
        source = build_source(state)
        message = FakeMessage("オーナー、元気？", guild=None)
        drive(source, message)
        self.assertTrue(state.calls, "沒有走到 handle（多半是 on_message 先拋例外）")
        self.assertEqual(message.replied, ["うん、聞こえてるよ"])

    def test_mention_in_guild_replies(self):
        """② 頻道裡 @ 她：一樣要回。"""
        state = FakeState()
        source = build_source(state)
        message = FakeMessage("ミラ、いる？", guild=object(),
                              mentions=[FakeUser(BOT_ID, name="mira", bot=True)])
        drive(source, message)
        self.assertEqual(message.replied, ["うん、聞こえてるよ"])

    def test_unaddressed_channel_message_is_quiet(self):
        """③ 沒 @ 她、也沒在延續她的話：旁聽，不回——但不能拋例外。"""
        state = FakeState(observed=True)
        source = build_source(state)
        message = FakeMessage("今日はいい天気だね", guild=object())
        drive(source, message)
        self.assertTrue(state.calls, "旁聽也要走到 handle")
        self.assertEqual(message.replied, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
