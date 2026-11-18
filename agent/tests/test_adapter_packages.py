#!/usr/bin/env python3
"""外圍適配器按職責拆成套件（review.md 2026-10-25 P3）。

判據（每個拆開的適配器一組）：
  ① canonical `adapter.*` 路徑直接匯出原本模組的公開名字；舊包不得復活；
  ② 每個職責住自己的模組；大類由職責 mixin 組成（方法逐字搬過去，見 commit 說明）；
  ③ 套件裡沒有一個檔超過 400 行。
"""

from __future__ import annotations

import importlib
import pathlib
import unittest

from demo_common import *  # noqa: F401,F403

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAX_LINES = 400


def _package_files(dotted: str) -> list[pathlib.Path]:
    return sorted((ROOT / pathlib.Path(*dotted.split("."))).glob("*.py"))


class _PackageCase(unittest.TestCase):
    package = ""
    exported: tuple[str, ...] = ()
    modules: dict[str, tuple[str, ...]] = {}

    def _check(self):
        pkg = importlib.import_module(self.package)
        for name in self.exported:
            self.assertTrue(hasattr(pkg, name), f"{self.package}.{name} 不見了")
        for module, names in self.modules.items():
            sub = importlib.import_module(f"{self.package}.{module}")
            for name in names:
                self.assertIs(getattr(sub, name), getattr(pkg, name, getattr(sub, name)),
                              f"{module}.{name}")
        for path in _package_files(self.package):
            lines = len(path.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(lines, MAX_LINES, f"{path.name}: {lines} 行")


class TestTrpgSeatPackage(_PackageCase):
    package = "agent.adapter.trpg"
    exported = ("TrpgConfig", "TrpgMcp", "TrpgSeat", "choose_action", "render_view")
    modules = {"config": ("TrpgConfig",), "mcp": ("TrpgMcp",),
               "decide": ("choose_action", "render_view"), "seat": ("TrpgSeat",),
               "mood": ("SeatMood", "GAME_AFFECT"), "report": ("SeatReporting",),
               "prompt": ("SeatPrompts",)}

    def test_package(self):
        self._check()

    def test_seat_is_composed_of_its_responsibilities(self):
        from agent.adapter.trpg.mood import SeatMood
        from agent.adapter.trpg.prompt import SeatPrompts
        from agent.adapter.trpg.report import SeatReporting
        from agent.adapter.trpg import TrpgSeat
        for mixin, method in ((SeatMood, "_maybe_decline"), (SeatReporting, "_maybe_report"),
                              (SeatPrompts, "_prompt")):
            self.assertTrue(issubclass(TrpgSeat, mixin))
            self.assertIs(getattr(TrpgSeat, method), getattr(mixin, method))


class TestTopLevelTrpgAdapter(unittest.TestCase):
    """S4-l：所有适配器只有一个顶层 canonical 路径，没有旧命名空间。"""

    def test_trpg_direct_imports_expose_canonical_objects(self):
        import agent.adapter.trpg as canonical
        import agent.adapter.trpg.hub as canonical_hub

        for name in ("TrpgConfig", "TrpgMcp", "TrpgSeat", "choose_action", "render_view"):
            self.assertTrue(hasattr(canonical, name), name)
        self.assertTrue(hasattr(canonical_hub, "TrpgHub"))
        self.assertTrue(hasattr(canonical_hub, "hub_for"))

    def test_legacy_package_is_gone_and_image_contains_adapter(self):
        self.assertFalse((ROOT / "blueagent_next").exists())
        service = (ROOT / "agent/brain/compose/trpg_service.py").read_text(encoding="utf-8")
        integration = (ROOT / "agent/server/integrations/trpg.py").read_text(encoding="utf-8")
        self.assertIn("from agent.adapter.trpg import hub as trpg_hub", service)
        self.assertIn("from agent.adapter.trpg import TrpgConfig, TrpgMcp, TrpgSeat", integration)
        dockerfile = (ROOT / "agent" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY agent/adapter ./agent/adapter", dockerfile)
        self.assertNotIn("COPY blueagent_next", dockerfile)


class TestDiscordSourcePackage(_PackageCase):
    package = "agent.adapter.discord_source"
    exported = ("DEFAULT_NAMES", "DiscordConfig", "DiscordRouter", "DiscordSource", "build_discord")
    modules = {"config": ("DiscordConfig", "DEFAULT_NAMES"), "routing": ("DiscordRouter",),
               "source": ("DiscordSource", "build_discord"),
               "transport": ("DiscordTransport",), "speech": ("DiscordSpeech",)}

    def test_package(self):
        self._check()

    def test_source_is_composed_of_its_responsibilities(self):
        from agent.adapter.discord_source import DiscordSource
        from agent.adapter.discord_source.speech import DiscordSpeech
        from agent.adapter.discord_source.transport import DiscordTransport
        for mixin, method in ((DiscordTransport, "start"), (DiscordTransport, "send_dm"),
                              (DiscordSpeech, "handle_voice"), (DiscordSpeech, "merge_wavs")):
            self.assertTrue(issubclass(DiscordSource, mixin))
            self.assertIs(getattr(DiscordSource, method), getattr(mixin, method))

    def test_routing_does_not_need_the_discord_client(self):
        # 訊息映射是純邏輯：不載入 discord.py 也能用（client 只在 transport 裡延遲 import）
        source = (ROOT / "agent/adapter/discord_source/routing.py").read_text(encoding="utf-8")
        self.assertNotIn("import discord", source)



class TestDiscordVoicePackage(_PackageCase):
    package = "agent.adapter.discord_voice"
    exported = ("DiscordVoiceSession", "Utterance", "UtteranceBuffer", "VoiceConfig", "build_voice",
                "pcm48_stereo_to_wav16", "rms_of", "VC_RATE", "VC_CHANNELS")
    modules = {"config": ("VoiceConfig", "VC_RATE"), "audio": ("UtteranceBuffer", "rms_of"),
               "session": ("DiscordVoiceSession", "build_voice"),
               "presence": ("VoicePresence",), "receive": ("VoiceReceive",),
               "playback": ("VoicePlayback",)}

    def test_package(self):
        self._check()

    def test_session_is_composed_of_its_responsibilities(self):
        from agent.adapter.discord_voice import DiscordVoiceSession
        from agent.adapter.discord_voice.playback import VoicePlayback
        from agent.adapter.discord_voice.presence import VoicePresence
        from agent.adapter.discord_voice.receive import VoiceReceive
        for mixin, method in ((VoicePresence, "join"), (VoiceReceive, "note_speech_start"),
                              (VoicePlayback, "say")):
            self.assertTrue(issubclass(DiscordVoiceSession, mixin))
            self.assertIs(getattr(DiscordVoiceSession, method), getattr(mixin, method))

    def test_audio_helpers_are_independent_of_the_session(self):
        # 音訊轉換／斷句是純函式：不依賴 session、也不需要 discord
        source = (ROOT / "agent/adapter/discord_voice/audio.py").read_text(encoding="utf-8")
        self.assertNotIn("DiscordVoiceSession", source)
        self.assertNotIn("import discord", source)

if __name__ == "__main__":
    unittest.main(verbosity=2)
