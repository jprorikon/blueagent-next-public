#!/usr/bin/env python3
"""組合根拆段之後的接線（2026-10-25 結構 review 第 3 階段）。

判據：
  ① 工具面的順序＝模型看到的順序：拆段前後一模一樣（拆之前實測過這份清單）；
  ② `build()` 本身只做接線：不超過 220 行（以前 628 行、抓十個區域變數的閉包都在裡面）；
  ③ 拆出去的心跳組裝、自主任務、記憶 sink 都還接得上（實際跑一次）。
"""

from __future__ import annotations

import ast
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.bootstrap import build as build_app

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: 沒設 HERMES_API_KEY ⇒ 沒有委任；MCP 沒設定檔 ⇒ Firecrawl 工具不進引擎 registry。
#: 2026-11-04 全量遷移：舊 web_search／web_fetch／web_browse 退役，搜尋／抓取自 Firecrawl MCP 進來。
#: `memory_open` 跟 `memory_search` 必須成對，順序刻意緊接在後面：search 吐 ptr、open 吃 ptr。
TOOL_ORDER = ["voice.call", "voice.speak", "voice.hangup", "playground", "weather_get",
              # 公共區（owner 2026-11-07）：activity_peek 已廢除；讀帖走現役 MCP `board`
              "notes_read",
              # 她自己那台 VM 的 shell（owner 2026-11-03；只在 work 模式出現）
              "terminal",
              "memory_search", "memory_open", "skill_view",
              # 時間感（owner 2026-10-27）：她替自己排「幾跳之後提某件事」
              "remind_in"]


class TestBootstrapWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = build_app(brain_delay=0, data_dir=tempfile.mkdtemp(prefix="ba-wire-"),
                            step_duration=0.02, life_pause=0.005)

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown_lives()

    def test_tool_order_is_unchanged(self):
        # 註冊順序（＝模型看到的相對順序）不變；按需工具全部解鎖時就是完整清單
        on_demand = {name for name in self.app.tools.snapshot()
                     if getattr(self.app.tools.get(name), "on_demand", False)}
        names = [schema["function"]["name"] for schema in
                 self.app.tools.schemas(mode="work", is_owner=True, capabilities={"delegate"},
                                        unlocked=on_demand)]
        self.assertEqual(names, TOOL_ORDER)

    def test_tools_are_tiered_by_channel(self):
        """分層（owner 2026-10-31）：常駐＋渠道限定一開始就給；天氣／網頁／筆記等要載入技能才給。"""
        def names(source, mode="chat", **kwargs):
            return [schema["function"]["name"] for schema in
                    self.app.tools.schemas(mode=mode, is_owner=True, source=source, **kwargs)]

        app = names("phone:owner")
        self.assertEqual(app, ["voice.call", "voice.speak", "voice.hangup",
                               "memory_search", "memory_open", "skill_view", "remind_in"])
        self.assertNotIn("weather_get", app)
        self.assertIn("weather_get", names("phone:owner", unlocked={"weather.get"}))
        # 通話裡：不打新電話、不另外「用語音回」，能掛
        call = names("phone:call")
        self.assertNotIn("voice.call", call)
        self.assertNotIn("voice.speak", call)
        self.assertIn("voice.hangup", call)
        # TRPG 桌：沒有任何語音工具
        self.assertFalse({"voice.call", "voice.speak", "voice.hangup"} & set(names("trpg:table-1")))
        # 執行權限跟清單一致：TRPG 桌上叫 voice.call 也不給跑
        tool = self.app.tools.get("voice.call")
        self.assertFalse(self.app.tools.allowed(tool, source="trpg:table-1"))
        self.assertTrue(self.app.tools.allowed(tool, source="phone:owner"))

    def test_public_sink_still_writes_her_experience(self):
        self.app.activity_log.append("mira", kind="study", text="規則書を三ページ読んだ",
                                     visibility="public", activity="自習", activity_id="a1")
        texts = [row["text"] for row in self.app.memory.snapshot()["facts"]]
        self.assertIn("規則書を三ページ読んだ", texts)

    def test_every_entity_has_a_life_and_a_heartbeat(self):
        self.assertEqual(sorted(self.app.lives), ["aster", "mira", "nori"])
        self.assertIsNotNone(self.app.heartbeat)
        self.assertTrue(callable(self.app.next_activity))


class TestBuildStaysSmall(unittest.TestCase):
    def test_build_function_is_only_wiring(self):
        source = (ROOT / "agent" / "brain" / "compose" / "bootstrap.py").read_text(encoding="utf-8")
        build = next(node for node in ast.parse(source).body
                     if isinstance(node, ast.FunctionDef) and node.name == "build")
        length = build.end_lineno - build.lineno + 1
        self.assertLessEqual(length, 220, f"build() 有 {length} 行：新的段落請放進 app/wiring 等模組")
        nested = [node.name for node in ast.walk(build)
                  if isinstance(node, ast.FunctionDef) and node is not build]
        self.assertEqual(nested, [], f"build() 裡不要再長閉包：{nested}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
