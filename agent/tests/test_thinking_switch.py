#!/usr/bin/env python3
"""思考開關的回歸（2026-10-24 查官方文件＋實測）。

官方（api-docs.deepseek.com/api/create-chat-completion）：
  · `thinking: {"type": "enabled"|"disabled"}`（預設 enabled）
  · `reasoning_effort: "none"|"low"|"high"|"max"`（預設 high；`none` 關思考）
  · `chat_template_kwargs` 不是它的參數——送了也被忽略。

實測（deepseek-flash，同一 prompt）：現在送法 2.80 s／reasoning 228；
`thinking disabled` 1.28 s／reasoning 0；`reasoning_effort=none` 1.11 s／reasoning 0。

⇒ 這支守兩件事：① 遠端要送 DeepSeek 認的鍵，本機端點仍走 chat_template_kwargs；
② 不能又退回「送了但沒生效」。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.config.llm import thinking_payload  # noqa: E402


class TestThinkingPayload(unittest.TestCase):
    DEEPSEEK = "https://api.deepseek.com/v1"
    LOCAL = "http://127.0.0.1:11435/v1"

    def test_remote_off_uses_thinking_disabled(self):
        body = thinking_payload(enable=False, base_url=self.DEEPSEEK)
        self.assertEqual(body, {"thinking": {"type": "disabled"}})
        self.assertNotIn("chat_template_kwargs", body, "DeepSeek 不認這個鍵")

    def test_local_off_uses_chat_template_kwargs(self):
        body = thinking_payload(enable=False, base_url=self.LOCAL)
        self.assertEqual(body, {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertNotIn("thinking", body, "本機 vLLM 沒有 DeepSeek 那個參數")

    def test_on_with_effort(self):
        self.assertEqual(thinking_payload(enable=True, effort="low", base_url=self.DEEPSEEK),
                         {"reasoning_effort": "low"})
        # 2026-11-02：白名單補進 medium（Strata 實測四個檔位都認）⇒ 原樣送、不再降成 high。
        # 舊註解「medium/xhigh 官方映射成 high」只對 DeepSeek 端點成立，這裡傳的是任意 base_url。
        self.assertEqual(thinking_payload(enable=True, effort="medium", base_url=self.DEEPSEEK),
                         {"reasoning_effort": "medium"})
        self.assertEqual(thinking_payload(enable=True, effort="xhigh", base_url=self.DEEPSEEK),
                         {"reasoning_effort": "high"}, "不認得的檔位退回 high")

    def test_on_without_effort_sends_nothing(self):
        self.assertEqual(thinking_payload(enable=True, base_url=self.DEEPSEEK), {})

    def test_brain_only_effort_keeps_side_channels_off(self):
        """`BLUEAGENT_BRAIN_REASONING_EFFORT=low` ⇒ 只有主腦開思考，其他呼叫點照樣關。"""
        from agent.brain.openai_compat import OpenAICompatibleConfig

        env = {"BLUEAGENT_LLM_ENABLE_THINKING": "0",
               "BLUEAGENT_BRAIN_REASONING_EFFORT": "low",
               "BLUEAGENT_LLM_BASE_URL": self.DEEPSEEK, "BLUEAGENT_LLM_API_KEY": "sk-test"}
        brain = OpenAICompatibleConfig.from_env(env)
        self.assertTrue(brain.enable_thinking)
        self.assertEqual(brain.reasoning_effort, "low")
        # 副通道用的那一組（同一個 env）仍然是關的
        self.assertEqual(thinking_payload(enable=False, base_url=self.DEEPSEEK),
                         {"thinking": {"type": "disabled"}})

    def test_brain_only_effort_none_means_off(self):
        from agent.brain.openai_compat import OpenAICompatibleConfig
        brain = OpenAICompatibleConfig.from_env({"BLUEAGENT_BRAIN_REASONING_EFFORT": "none"})
        self.assertFalse(brain.enable_thinking)

    def test_brain_body_carries_the_switch(self):
        """大腦本體也要真的帶上（只改 helper 沒接線等於沒修）。"""
        from agent.brain.openai_compat import (OpenAICompatibleBrain,
                                                        OpenAICompatibleConfig)
        from agent.brain.domain.entity import EntityRegistry
        from agent.brain.domain.principal import PrincipalRegistry
        from agent.brain.ports import BrainRequest

        brain = OpenAICompatibleBrain(
            OpenAICompatibleConfig(base_url=self.DEEPSEEK, api_key="sk-test",
                                   model="deepseek-flash", enable_thinking=False),
            EntityRegistry(), PrincipalRegistry())
        request = BrainRequest(thread_id="t", entity="mira", principal="owner",
                               text="やあ", meta={})
        body = brain._body(request, [{"role": "user", "content": "やあ"}])   # noqa: SLF001
        self.assertEqual(body.get("thinking"), {"type": "disabled"})
        self.assertNotIn("chat_template_kwargs", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
