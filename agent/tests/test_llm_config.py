#!/usr/bin/env python3
"""LLM 角色設定（`config/llm.py`）：一個檔決定每個角色用哪顆模型。

判據：
  ① 沒有 `llm.json` ⇒ 完全照 env（行為不變，壞檔不會炸服務）；
  ② 檔案優先、env 次之、最後才內建預設；角色專屬 env（`BLUEAGENT_LLM_STATE_MODEL`）可單獨覆寫；
  ③ `apply_env_defaults()` 只補沒設的欄位（不覆蓋 compose 明確給的值）；
  ④ 提煉／看圖真的吃到角色設定（不是只讀檔擺著）。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
import unittest.mock

from demo_common import *  # noqa: F401,F403

from agent.brain.config import llm as llm_config


class TestLLMConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def write(self, body) -> None:
        (self.root / "llm.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    def test_env_only_when_no_file(self):
        env = {"BLUEAGENT_LLM_MODEL": "ds-x", "BLUEAGENT_LLM_BASE_URL": "https://x/v1"}
        row = llm_config.settings_for("chat", env=env, root=self.root)
        self.assertEqual(row["model"], "ds-x")
        self.assertEqual(row["base_url"], "https://x/v1")
        self.assertEqual(row["role"], "chat")

    def test_env_overrides_file_but_file_fills_gaps(self):
        self.write({"roles": {"distill": {"model": "ds-file", "max_tokens": 999}}})
        env = {"BLUEAGENT_LLM_MODEL": "ds-env"}
        row = llm_config.settings_for("distill", env=env, root=self.root)
        self.assertEqual(row["model"], "ds-env", "env 是臨時覆寫，優先於檔案")
        self.assertEqual(row["max_tokens"], 999, "檔案補上 env 沒給的欄位")

    def test_role_env_beats_file(self):
        self.write({"roles": {"state": {"model": "ds-file"}}})
        env = {"BLUEAGENT_LLM_STATE_MODEL": "ds-role-env"}
        row = llm_config.settings_for("state", env=env, root=self.root)
        self.assertEqual(row["model"], "ds-role-env")

    def test_broken_file_falls_back_to_env(self):
        (self.root / "llm.json").write_text("{ not json", encoding="utf-8")
        row = llm_config.settings_for("chat", env={"BLUEAGENT_LLM_MODEL": "ds-env"}, root=self.root)
        self.assertEqual(row["model"], "ds-env")

    def test_apply_env_defaults_does_not_override(self):
        self.write({"roles": {"chat": {"model": "ds-file"}}})
        env = {"BLUEAGENT_LLM_MODEL": "ds-explicit"}
        llm_config.apply_env_defaults(env=env, root=self.root)
        self.assertEqual(env["BLUEAGENT_LLM_MODEL"], "ds-explicit")
        env2: dict = {}
        llm_config.apply_env_defaults(env=env2, root=self.root)
        self.assertEqual(env2["BLUEAGENT_LLM_MODEL"], "ds-file")

    def test_summary_lists_all_roles(self):
        rows = llm_config.summary(env={}, root=self.root)
        roles = {row["role"] for row in rows}
        self.assertTrue({"chat", "state", "subagent", "distill", "vision"} <= roles, rows)


class TestWiring(unittest.TestCase):
    def test_distill_uses_role_settings(self):
        from agent.brain.runtime.distill import Distiller

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / "llm.json").write_text(json.dumps(
            {"roles": {"distill": {"model": "ds-distill", "max_tokens": 321,
                                   "temperature": 0.05}}}), encoding="utf-8")
        old = llm_config.config_path
        llm_config.config_path = lambda root_=None: root / "llm.json"   # noqa: ARG005
        self.addCleanup(lambda: setattr(llm_config, "config_path", old))
        # 整包測試裡別的測試可能設過通用 env（env 優先於檔案）⇒ 這裡先把那兩個變數拿掉
        import os as _os
        patcher = unittest.mock.patch.dict(_os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("BLUEAGENT_LLM_MODEL", "DEEPSEEK_MODEL", "BLUEAGENT_LLM_DISTILL_MODEL"):
            _os.environ.pop(key, None)
        distiller = Distiller(memory=None, raw_log=type("L", (), {"root": root / "raw"})(),
                              transport=lambda prompt: "{}", entities=[])
        self.assertEqual(distiller.model, "ds-distill")
        self.assertEqual(distiller.max_tokens, 321)
        self.assertAlmostEqual(distiller.temperature, 0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
