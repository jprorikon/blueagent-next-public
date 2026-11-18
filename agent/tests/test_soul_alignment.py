#!/usr/bin/env python3
"""人格檔对齐（照現役橋）：`SOUL.md` ＋ `<角色>.md`。

判據：
  ① 組出來的塊與現役橋同形：`[[soul:<id>]] … [[/soul]]`；
  ② 塊裡有日語語言硬規則（「常に日本語」）與角色檔內容（「ミラ」）；
  ③ 貼的位置＝**固定段 system，緊接在場景規則之後、歷史之前**（2026-11-02 改，吃前綴快取）；
     不進 history、不進最後一條用戶訊息；語言鎖仍在最後一條的尾端；
  ④ 有 soul 時**不疊** Demo 那份中文人格（兩份人格＝兩種語言，模型會挑中文）；
  ⑤ repo 內 `agent/persona/` 真的在（檔案被誤刪時這裡會紅）。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from types import SimpleNamespace

from demo_common import *  # noqa: F401,F403

from agent.brain.messages import build_messages
from agent.brain.compose import stance_seed
from agent.brain.compose.entities import (build_entities, build_principals,
                                          build_social)
from agent.brain.compose.stages import PersonaStage
from agent.brain.domain.event import IngressEvent, InputKind, Origin, SourceClass
from agent.brain.persona.soul import SoulLibrary
from agent.brain.ports import BrainRequest
from agent.brain.runtime.stance import StanceService
from agent.brain.runtime.stance_store import StanceStore

REPO = pathlib.Path(__file__).resolve().parents[2]
SOUL_DIR = REPO / "agent" / "persona"


def make_stage(soul_root: pathlib.Path | None = None, speak_lang: str = "") -> PersonaStage:
    stances = StanceService(StanceStore(pathlib.Path(tempfile.mkdtemp(prefix="ba-soul-stance-"))),
                            stance_seed.build_catalog())
    stance_seed.seed(stances)
    soul = SoulLibrary(soul_root) if soul_root is not None else None
    return PersonaStage(build_entities(), build_principals(), build_social(),
                        stances, speak_lang=speak_lang, soul=soul)


def make_env():
    event = IngressEvent(principal="owner", entity="mira", source="phone:owner",
                         kind=InputKind.TEXT, at=0.0, source_class=SourceClass.INTERACTIVE,
                         origin=Origin.OWNED, participants=("owner",))
    return SimpleNamespace(event=event,
                           decision=SimpleNamespace(entity="mira", principal="owner",
                                                    scope="", addressed_to=""),
                           thread=SimpleNamespace(transcript=[], id="th_1"),
                           meta={}, text="你好呀")


class TestSoulLibrary(unittest.TestCase):
    def test_repo_ships_the_persona_files(self):
        self.assertTrue((SOUL_DIR / "SOUL.md").exists(), "agent/persona/SOUL.md 不見了")
        self.assertTrue((SOUL_DIR / "mira.md").exists())

    def test_core_keeps_the_language_rule(self):
        core = SoulLibrary(SOUL_DIR).core()
        self.assertIn("常に日本語", core)
        self.assertIn("中国語で話しかけても", core)

    def test_block_is_wrapped_like_the_bridge(self):
        block = SoulLibrary(SOUL_DIR).block("mira.md")
        self.assertTrue(block.startswith("[[soul:mira]]"))
        self.assertTrue(block.endswith("[[/soul]]"))
        self.assertIn("常に日本語", block)          # 共用核心
        self.assertIn("ミラ", block)          # 角色檔
        # 別的角色檔不該被讀到（SOUL.md 自己會舉 /soul/aster.md 當例子，所以比對角色檔的內容）
        self.assertNotIn("アスター", block)

    def test_missing_files_degrade_to_empty(self):
        library = SoulLibrary(pathlib.Path(tempfile.mkdtemp(prefix="ba-empty-soul-")))
        self.assertEqual(library.block("mira"), "")
        self.assertIn("SOUL.md", library.missing)
        self.assertIn("mira.md", library.missing)


class TestPersonaStageWithSoul(unittest.TestCase):
    def test_soul_block_and_no_chinese_persona(self):
        env = make_env()
        make_stage(SOUL_DIR).run(env, None)
        self.assertIn("[[soul:mira]]", env.meta["soul_block"])
        self.assertIn("常に日本語", env.meta["soul_block"])
        # Demo 的中文人格不能再疊上來
        self.assertNotIn("你是ミラ", env.meta["persona"])

    def test_without_soul_has_no_demo_persona(self):
        # 2026-10-25：demo 的中文 PERSONAS 不再當退路（生產一定有人格檔；沒有就不給人格）
        env = make_env()
        make_stage(None).run(env, None)
        self.assertNotIn("soul_block", env.meta)
        self.assertNotIn("你是ミラ", env.meta["persona"])


class TestMessagePlacement(unittest.TestCase):
    def request(self, soul_block: str = "", speak_lang: str = "") -> BrainRequest:
        return BrainRequest(thread_id="t", entity="mira", principal="owner",
                            text="你好呀", system="场景规则",
                            meta={"recall": [], "soul_block": soul_block,
                                  "speak_lang": speak_lang,
                                  "transcript": [{"role": "user", "text": "上一句",
                                                  "speaker": "owner"}]})

    def test_soul_sits_in_fixed_block_before_history(self):
        block = "[[soul:mira]]\n人格\n[[/soul]]"
        messages = build_messages(self.request(block))
        self.assertEqual(messages[0]["content"], "场景规则")
        self.assertEqual(messages[1]["role"], "system")
        self.assertTrue(messages[1]["content"].startswith(block))
        self.assertIn("[[mode:chat]]", messages[1]["content"])
        history_at = next(index for index, row in enumerate(messages) if row["content"] == "上一句")
        self.assertGreater(history_at, 1, "人格在歷史之前（固定段）")
        self.assertEqual(sum(row["content"] == "上一句" for row in messages), 1,
                         "history 原文要原樣留著，且只有一條")
        self.assertTrue(messages[-1]["content"].endswith("你好呀"))
        self.assertFalse(any(block in row.get("content", "") for row in messages[2:]),
                         "人格只出現在固定段一次")

    def test_soul_survives_char_budget_trimming(self):
        block = "[[soul:mira]]\n" + ("人格" * 400) + "\n[[/soul]]"
        messages = build_messages(self.request(block), max_chars=2000)
        self.assertIn("[[soul:mira]]", messages[1]["content"])

    def test_soul_then_language_suffix_both_apply(self):
        block = "[[soul:mira]]\n人格\n[[/soul]]"
        messages = build_messages(self.request(block, "ja"))
        self.assertTrue(messages[1]["content"].startswith(block))
        self.assertTrue(messages[-1]["content"].endswith("（必ず日本語で答えて）"))




class TestFormByChannel(unittest.TestCase):
    """owner 2026-11-11：App と声（TTS）は短い形を保つ、Discord の文字だけは縛らない。

    以前は SOUL.md §2 に「最初の一文 15 字・全体 1〜3 文・Markdown 不可」を固定で書いていた
    ⇒ Discord の文字にも同じ縛り。いまは回ごとに `[[form]]` 段で付ける（通話は `[[call]]` が同じ役）。
    """

    def blocks(self, **meta) -> str:
        request = BrainRequest(thread_id="t", entity="mira", principal="owner", text="やあ",
                               meta={"recall": [], **meta})
        return "\n".join(str(row.get("content") or "") for row in build_messages(request))

    def test_app_and_voice_keep_the_short_form(self):
        for channel in ("app", "discord_vc", "trpg"):
            with self.subTest(channel=channel):
                self.assertIn("[[/form]]", self.blocks(channel=channel))

    def test_discord_text_is_not_limited(self):
        self.assertNotIn("[[/form]]", self.blocks(channel="discord"))

    def test_app_group_gets_the_one_liner_form(self):
        """M5 §10（2026-11-17）：群聊發言字數眾數要落在 6–10 字 ⇒ App 群聊換「ひと言」的形。"""
        group = self.blocks(channel="app", group=True)
        self.assertIn("グループの雑談", group)
        self.assertIn("10 文字前後", group)
        self.assertNotIn("全体で 1〜3 文", group, "群聊不再套一對一的三句形")
        self.assertNotIn("グループの雑談", self.blocks(channel="app"), "一對一照舊")
        self.assertNotIn("[[/form]]", self.blocks(channel="discord", group=True), "Discord 文字群照舊不縛")

    def test_call_uses_its_own_block_and_unknown_gets_none(self):
        text = self.blocks(channel="call", in_call=True)
        self.assertIn("[[call]]", text)
        self.assertNotIn("[[/form]]", text)
        self.assertNotIn("[[/form]]", self.blocks(), "沒有渠道（自主工作那類）不套形")

    def test_soul_no_longer_hardcodes_the_voice_form(self):
        soul = (SOUL_DIR / "SOUL.md").read_text(encoding="utf-8")
        self.assertNotIn("15 文字以内", soul)
        self.assertIn("[[form]]", soul, "SOUL 說明形由每回合的段決定")

class TestNoBridgeVocabulary(unittest.TestCase):
    """人格檔用 blueagent-next 自己的說法（owner 2026-11-11「和现有的 blueagentnext 进行表述统一」）。

    殻／橋／Hermes／MEDIA:／adb 都是舊 Hermes 橋的東西：這套系統裡貼人格檔的是「システム」，
    委任是「委任の道具」，送檔案是「送るための道具」——舊說法進 prompt 只會讓她以為有不存在的路。
    """

    WORDS = ("殻", "殼", "壳", "橋", "Hermes", "hermes", "MEDIA:", "publish_media", "adb", "phone_js")

    def test_persona_files_have_no_bridge_words(self):
        hits = [f"{path.name}:{word}" for path in sorted(SOUL_DIR.glob("*.md"))
                for word in self.WORDS if word in path.read_text(encoding="utf-8")]
        self.assertEqual(hits, [])

if __name__ == "__main__":
    unittest.main(verbosity=2)
