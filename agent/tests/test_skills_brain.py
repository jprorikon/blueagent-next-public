#!/usr/bin/env python3
"""技能整理／學習「單獨走 DeepSeek ＋ 超長上下文」（owner 2026-11-03）。

判據：
  ① `skills-tidy`（或失敗 ≥2 次的復盤）那一步會用 `alt_brain`，其他步驟照舊用本地；
  ② 它拿到的是**長上下文**：notes 30 天／40 段、digest 4,000 字、前 40 步歷史；
  ③ 給技能步驟的指示裡明寫「本文も必ず日本語」——雲端模型容易滑回中文；
  ④ 雲端那次呼叫炸掉 ⇒ **回落本地同一份 prompt**，不讓這一步變成空的。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.runtime import self_work  # noqa: E402
from agent.brain.runtime.self_work import run_step  # noqa: E402


class _Tool:
    def __init__(self, name, schema=None):
        self.name = name
        self.schema = schema or {"type": "function",
                                 "function": {"name": name, "description": name, "parameters": {}}}


class _Notes:
    """registry 的替身：`notes.read` 真的被呼叫時記下來（其他工具不該被碰）。"""

    calls: list = []

    def __init__(self):
        self.calls = []

    def run(self, name, args, context=None):
        self.calls.append((name, args))
        return type("R", (), {"ok": True, "data": {"count": 3, "text": "ノート本文"},
                              "error": ""})()


class _Registry:
    """帶 tool 面（schema／allowed）＋ notes 記錄。"""

    def __init__(self):
        self._items = {name: _Tool(name) for name in
                       ("notes.read", "mcp_skills_write", "mcp_skills_list")}
        self.calls = []

    def get(self, name):
        return self._items.get(name)

    def allowed(self, tool, **kwargs):
        return True

    def values(self):
        return list(self._items.values())

    def run(self, name, args, context=None):
        self.calls.append((name, args))
        if name != "notes.read":
            raise AssertionError(f"不該在這一輪跑 {name}")
        return type("R", (), {"ok": True, "data": {"count": 3, "text": "ノート本文"},
                              "error": ""})()


class _Brain:
    def __init__(self, *, text="できた", boom=False):
        self.text = text
        self.boom = boom
        self.seen = []

    def run_turn(self, request):
        self.seen.append(str(request.text or ""))
        if self.boom:
            raise RuntimeError("cloud down")
        yield ("done", {"text": self.text})


class TestSkillsBrain(unittest.TestCase):
    def setUp(self):
        self.tools = _Registry()
        self.context = lambda: type("C", (), {})()

    def _run(self, brain, alt=None, long_context=False, step="技能の目次を見直す"):
        return run_step(brain, self.tools, entity="nori", activity="技能を見直す", step=step,
                        alt_brain=alt, alt_reason="skills:deepseek" if alt else "",
                        long_context=long_context, context_factory=self.context)

    def test_alt_brain_used_and_local_untouched(self):
        local, cloud = _Brain(text="ローカル"), _Brain(text="クラウド")
        result = self._run(local, alt=cloud, long_context=True, step="自分のノートを読み返す")
        self.assertEqual(result["text"], "クラウド")
        self.assertEqual(len(local.seen), 0, "本地腦不該被叫到")

    def test_long_context_uses_bigger_notes_window(self):
        run_step(_Brain(), self.tools, entity="nori", activity="技能を見直す", step="ノートを読み返す",
                 long_context=True, context_factory=self.context)
        self.assertEqual(self.tools.calls[0][1], {"days": 30, "limit": 40})

    def test_normal_context_keeps_default_window(self):
        run_step(_Brain(), self.tools, entity="nori", activity="何か", step="ノートを読み返す",
                 context_factory=self.context)
        self.assertEqual(self.tools.calls[0][1], {"days": 14, "limit": 24})

    def test_cloud_failure_falls_back_to_local(self):
        local, cloud = _Brain(text="ローカルの答え"), _Brain(boom=True)
        result = self._run(local, alt=cloud)
        self.assertEqual(result["text"], "ローカルの答え")
        self.assertEqual(len(local.seen), 1, "雲端掛了要用同一份 prompt 重跑本地")

    def test_skills_step_asks_japanese(self):
        cloud = _Brain()
        run_step(cloud, self.tools, entity="nori", activity="技能を見直す",
                 step="目次を開く\n（技能の本文も必ず日本語で書く。中国語を混ぜない）",
                 alt_brain=cloud, alt_reason="skills:deepseek", long_context=True,
                 context_factory=self.context)
        self.assertIn("日本語", cloud.seen[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
