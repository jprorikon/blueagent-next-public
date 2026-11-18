#!/usr/bin/env python3
"""階段之間的資料契約（review.md 2026-10-25 P2）：`env.meta` 的每個鍵都要宣告過。

判據（掃 `app/stages/*.py`，原始的 `env.meta[...]` 與有型別的 `turn.<段>.<欄位>` 都算）：
  ① 用到的鍵都在 `app/stages/context.py` 宣告過（打錯字、新鍵沒宣告 ⇒ 紅）；
  ② 寫入只來自宣告的 writers（別的階段偷寫 ⇒ 紅）；
  ③ 有型別視圖跟 `env.meta` 是同一份資料：沒寫回預設值、寫了兩邊都看得到、清單用專用方法累加；
  ④ **所有階段**都只走有型別視圖：除了 context.py 本身，沒有任何階段直接碰 `env.meta`
     （2026-10-25 遷移完成；新程式碼直接寫 `env.meta` 會紅）。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.stages.context import TurnContext, declared_fields

ROOT = pathlib.Path(__file__).resolve().parents[2]
STAGES = ROOT / "agent" / "brain" / "compose" / "stages"
SECTION_ATTRS = {"intake", "recall", "persona", "state", "overrides", "output"}
ADDERS = {"add_tool_result": "tool_results", "add_memory_hits": "memory_hits",
          "add_sticker": "stickers"}


def _is_env_meta(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "meta"
            and isinstance(node.value, ast.Name) and node.value.id == "env")


def _section_aliases(tree) -> set[str]:
    """`persona = context.persona` 這種別名（之後 `persona.mode = …` 也要算進契約）。"""
    return {target.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute) and node.value.attr in SECTION_ATTRS
            for target in node.targets if isinstance(target, ast.Name)}


def _is_section(node, aliases: set[str]) -> bool:
    """`<x>.persona` 這種段落，或段落的別名。"""
    return ((isinstance(node, ast.Attribute) and node.attr in SECTION_ATTRS)
            or (isinstance(node, ast.Name) and node.id in aliases))


def _accesses(path: pathlib.Path):
    """回 `[(key, is_write, lineno)]`。"""
    out = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _section_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_env_meta(node.value) \
                and isinstance(node.slice, ast.Constant):
            out.append((node.slice.value, isinstance(node.ctx, (ast.Store, ast.Del)), node.lineno))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and _is_env_meta(node.func.value):
            method = node.func.attr
            if method in ("get", "setdefault", "pop") and node.args \
                    and isinstance(node.args[0], ast.Constant):
                out.append((node.args[0].value, method != "get", node.lineno))
            elif method == "update" and node.args and isinstance(node.args[0], ast.Dict):
                out += [(key.value, True, node.lineno) for key in node.args[0].keys
                        if isinstance(key, ast.Constant)]
        # 有型別：turn.<段>.<欄位>／別名.<欄位>（讀或寫）、….output.add_*()
        elif isinstance(node, ast.Attribute) and _is_section(node.value, aliases):
            if node.attr in ADDERS:
                out.append((ADDERS[node.attr], True, node.lineno))
            elif not node.attr.startswith("_") and node.attr != "has":
                out.append((node.attr, isinstance(node.ctx, ast.Store), node.lineno))
    return out


class TestStageContract(unittest.TestCase):
    def setUp(self):
        self.fields = declared_fields()

    def test_every_key_is_declared(self):
        unknown = []
        for path in sorted(STAGES.glob("*.py")):
            if path.name in {"context.py", "__init__.py"}:
                continue
            for key, _write, line in _accesses(path):
                if key not in self.fields:
                    unknown.append(f"{path.name}:{line} {key}")
        self.assertEqual(unknown, [], "新鍵請先在 app/stages/context.py 宣告")

    def test_writes_only_from_declared_writers(self):
        bad = []
        for path in sorted(STAGES.glob("*.py")):
            if path.name in {"context.py", "__init__.py"}:
                continue
            module = path.stem
            for key, write, line in _accesses(path):
                field = self.fields.get(key)
                if write and field is not None and module not in field.writers:
                    bad.append(f"{path.name}:{line} 寫 {key}（宣告的 writers：{field.writers}）")
        self.assertEqual(bad, [])

    def test_no_stage_touches_env_meta_directly(self):
        raw = []
        for path in sorted(STAGES.glob("*.py")):
            if path.name == "context.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            raw += [f"{path.name}:{node.lineno}" for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute) and _is_env_meta(node)]
        self.assertEqual(raw, [], "階段之間的資料請走 TurnContext（app/stages/context.py）")


class TestTypedViewIsTheSameData(unittest.TestCase):
    def test_defaults_writes_and_adders(self):
        meta: dict = {}
        turn = TurnContext(meta)
        self.assertEqual(turn.persona.persona, "")
        self.assertTrue(turn.persona.is_owner, "沒寫過 ⇒ 宣告的預設值")
        self.assertEqual(turn.recall.recall, [])
        self.assertFalse(turn.persona.has("entity_name"))
        turn.output.reply_text = "やあ"
        self.assertEqual(meta["reply_text"], "やあ", "寫進同一個 env.meta")
        meta["mode"] = "work"
        self.assertEqual(turn.persona.mode, "work", "舊程式碼寫的值，新視圖讀得到")
        turn.output.add_tool_result({"ok": True})
        turn.output.add_tool_result({"ok": False})
        turn.output.add_memory_hits(["a", "b"])
        self.assertEqual(len(meta["tool_results"]), 2)
        self.assertEqual(meta["memory_hits"], ["a", "b"])

    def test_keys_are_unique_across_sections(self):
        self.assertGreaterEqual(len(declared_fields()), 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
