#!/usr/bin/env python3
"""心跳的兩支小活要走**窄模型**，不要跟主腦搶同一條推論 slot（owner 2026-11-02）。

病歷：2026-11-02 22:08 主人在 App 說一句，`recall 154ms → subagents 5904ms → brain 38ms`、
`first_token=null`、出口是空字串。主腦那台 125B **只有一條推論 slot**；而心情定位
（`MoodLine`）與內心獨白（`Monologue`）當時是直接吃 `BLUEAGENT_LLM_*`＝主腦那一組，
每 75 秒一跳就可能撞進他的回合。

判據：
  ① `settings_for("moodline"/"monologue")` 吃得到角色專屬 env（`BLUEAGENT_LLM_<ROLE>_*`）；
  ② `http_call(role=…)` 真的打到那個 role 的 base_url／model（不是主腦那組）；
  ③ `http_call()` 不給 role ⇒ 行為一個字都沒變（還是 `BLUEAGENT_LLM_*`）。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.brain.config import llm as llm_config
from agent.brain.persona.core import http_call

#: 主腦（125B）＋兩支窄模型，照 compose 現在的形狀
ENV = {
    "BLUEAGENT_LLM_MODEL": "swift-1.5-iq2_xs",
    "BLUEAGENT_LLM_BASE_URL": "http://192.168.50.36:8080/v1",
    "BLUEAGENT_LLM_MOODLINE_MODEL": "qwen35-4b",
    "BLUEAGENT_LLM_MOODLINE_BASE_URL": "http://192.168.50.36:8093/v1",
    "BLUEAGENT_LLM_MONOLOGUE_MODEL": "qwen35-9b",
    "BLUEAGENT_LLM_MONOLOGUE_BASE_URL": "http://192.168.50.36:8091/v1",
}


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _fake_urlopen(calls: list[dict]):
    def urlopen(request, timeout=None):
        calls.append({"url": request.full_url,
                      "body": json.loads((request.data or b"{}").decode())})
        return _Response({"choices": [{"message": {"content": "はい"}}]})
    return urlopen


class TestInnerRolesUseNarrowModel(unittest.TestCase):
    def test_role_settings_read_the_role_env(self):
        mood = llm_config.settings_for("moodline", env=ENV)
        self.assertEqual(mood["model"], "qwen35-4b")
        self.assertEqual(mood["base_url"], "http://192.168.50.36:8093/v1")
        mono = llm_config.settings_for("monologue", env=ENV)
        self.assertEqual(mono["model"], "qwen35-9b")
        self.assertEqual(mono["base_url"], "http://192.168.50.36:8091/v1")

    def test_http_call_with_role_hits_the_narrow_endpoint(self):
        calls: list[dict] = []
        with mock.patch("urllib.request.urlopen", _fake_urlopen(calls)):
            call = http_call(ENV, max_tokens=300, disable_thinking=True, role="moodline")
            self.assertEqual(call("心情をひとことで"), "はい")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], "http://192.168.50.36:8093/v1/chat/completions")
        self.assertEqual(calls[0]["body"]["model"], "qwen35-4b",
                         "心情定位不該再打到主腦那顆 125B")

    def test_http_call_without_role_is_unchanged(self):
        calls: list[dict] = []
        with mock.patch("urllib.request.urlopen", _fake_urlopen(calls)):
            call = http_call(ENV, max_tokens=300, disable_thinking=True)
            call("そのまま")
        self.assertEqual(calls[0]["url"], "http://192.168.50.36:8080/v1/chat/completions",
                         "沒給 role 的呼叫端（人格編譯等）行為必須一個字都不變")
        self.assertEqual(calls[0]["body"]["model"], "swift-1.5-iq2_xs")

    def test_unknown_role_falls_back_to_brain_settings(self):
        # 打錯字／漏設 ⇒ 退回主腦那組，而不是炸掉或打到空位址
        item = llm_config.settings_for("moodline_typo", env={"BLUEAGENT_LLM_BASE_URL":
                                                             "http://192.168.50.36:8080/v1"})
        self.assertEqual(item["base_url"], "http://192.168.50.36:8080/v1")


if __name__ == "__main__":
    unittest.main()
