#!/usr/bin/env python3
"""大腦＝chat 角色的接線（2026-11-08 的坑）：**模型與端點必須跟著角色表**。

為什麼要有這支：`build_brain()` 以前只把 max_tokens／temperature／timeout 從角色表搬進來，
model／base_url 仍讀通用 env ⇒ 改 `data/llm.json` 的 chat 看起來沒生效；
2026-11-08 通話切不回 deepseek-flash 就是這個原因（brain 還在本機 125B 上打，首字 3–7 s 被砍）。

生產現場（compose）三層都在：llm.json 的 chat、`BLUEAGENT_LLM_CHAT_*`（角色專屬）、
`BLUEAGENT_LLM_*`（通用，指著本機給非主腦角色）。本檔就把這三層照生產擺一次。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from agent.brain import factory
from agent.brain.domain.entity import EntityRegistry
from agent.brain.domain.principal import PrincipalRegistry


class BrainRoleWiringCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-llm-role-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "llm.json").write_text(json.dumps({"roles": {
            "chat": {"model": "deepseek-flash", "base_url": "https://api.deepseek.com/v1",
                     "max_tokens": 3200, "temperature": 0.8},
            "subagent": {"model": "qwen35-9b", "base_url": "http://192.168.50.36:8091/v1"},
        }}), encoding="utf-8")
        # 通用 env 指著本機（給非主腦的角色用）——不該再蓋掉主腦
        self.base_env = {
            "BLUEAGENT_BRAIN": "deepseek",
            "BLUEAGENT_DATA_DIR": str(self.root),
            "BLUEAGENT_LLM_CONFIG": str(self.root / "llm.json"),
            "DEEPSEEK_API_KEY": "sk-test",
            "BLUEAGENT_LLM_MODEL": "swift-1.5-iq2_xs",
            "BLUEAGENT_LLM_BASE_URL": "http://192.168.50.36:8080/v1",
            # 生產現場的角色專屬覆寫（compose 那兩行）
            "BLUEAGENT_LLM_CHAT_MODEL": "deepseek-flash",
            "BLUEAGENT_LLM_CHAT_BASE_URL": "https://api.deepseek.com/v1",
        }

    def build(self, **extra):
        env = dict(self.base_env, **extra)
        with mock.patch.dict("os.environ", env, clear=True):
            return factory.build_brain(EntityRegistry(), PrincipalRegistry())

    def test_brain_follows_chat_role_not_generic_env(self):
        brain = self.build()
        self.assertEqual(brain.config.model, "deepseek-flash")
        self.assertEqual(brain.config.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(brain.config.max_tokens, 3200)

    def test_file_alone_decides_when_no_role_env(self):
        """沒有角色專屬 env 時，主腦照 `data/llm.json` 的 chat（不是通用 env）。"""
        brain = self.build(BLUEAGENT_LLM_CHAT_MODEL="", BLUEAGENT_LLM_CHAT_BASE_URL="",
                           BLUEAGENT_LLM_MODEL="", BLUEAGENT_LLM_BASE_URL="")
        self.assertEqual(brain.config.model, "deepseek-flash")
        self.assertEqual(brain.config.base_url, "https://api.deepseek.com/v1")

    def test_generic_env_still_works_when_nothing_explicit(self):
        """沒有 llm.json、也沒有 `BLUEAGENT_LLM_CHAT_*` ⇒ 不拿內建預設硬蓋，照通用 env 走。"""
        brain = self.build(BLUEAGENT_LLM_CONFIG=str(self.root / "missing.json"),
                           BLUEAGENT_LLM_CHAT_MODEL="", BLUEAGENT_LLM_CHAT_BASE_URL="")
        self.assertEqual(brain.config.model, "swift-1.5-iq2_xs")
        self.assertEqual(brain.config.base_url, "http://192.168.50.36:8080/v1")

    def test_build_env_argument_is_the_env(self):
        """`build_brain(env=...)` 的角色專屬覆寫要從那份 env 讀，不是偷看 `os.environ`。"""
        with mock.patch.dict("os.environ", {"BLUEAGENT_LLM_CONFIG": str(self.root / "missing.json")},
                             clear=True):
            brain = factory.build_brain(EntityRegistry(), PrincipalRegistry(), env={
                "BLUEAGENT_BRAIN": "deepseek", "DEEPSEEK_API_KEY": "sk-test",
                "BLUEAGENT_LLM_MODEL": "swift-1.5-iq2_xs",
                "BLUEAGENT_LLM_CHAT_MODEL": "deepseek-flash"})
        self.assertEqual(brain.config.model, "deepseek-flash")

    def test_remote_endpoint_without_key_is_refused(self):
        with mock.patch.dict("os.environ", {**self.base_env, "DEEPSEEK_API_KEY": ""}, clear=True):
            with self.assertRaises(ValueError):
                factory.build_brain(EntityRegistry(), PrincipalRegistry())


if __name__ == "__main__":
    unittest.main()
