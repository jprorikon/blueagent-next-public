#!/usr/bin/env python3
"""作品檔要是**一件一個檔、裡面是成品**（owner 2026-11-08）。

實測：主人說「観測表、ファイルで送って」，她送來 679 bytes 的
`…-その下に使い方を三行で書く.md`——內容是「観測表の欄は左から、日時、見たこと…」這種
**表的說明**，不是表。查下去兩個成因：

  ① `form="work"` 的每一步各寫一個檔 ⇒「観測表」被拆成三份半成品（欄定義／記入例／使い方）
  ② 模型把作品寫成散文摘要（「欄は〜である」），沒有真的排出表格

判據：
  A. 同一天的同一件作品**只有一個檔**，每一步是裡面的 `## 節`
  B. 同一步重跑換掉那一節（不堆檔），其他節保留
  C. 作品的 prompt 會明講「寫現物（表就排 Markdown 表），不要寫說明」
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.runtime.life_step import _write_note  # noqa: E402
from agent.brain.runtime.inner.work_output import merge_section, parse_sections  # noqa: E402


class TestWorkMerge(unittest.TestCase):
    def test_sections_merge_and_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "w.md"
            merge_section(path, title="観測表", step="欄を決める", body="| 欄 | 意味 |")
            merge_section(path, title="観測表", step="記入例", body="| 日時 | 2026-11-07 |")
            merge_section(path, title="観測表", step="欄を決める", body="| 欄 | 意味 | 記入例 |")
            title, sections = parse_sections(path.read_text(encoding="utf-8"))
            self.assertEqual(title, "観測表")
            self.assertEqual([name for name, _ in sections], ["欄を決める", "記入例"])
            self.assertIn("記入例", sections[0][1], "同一步重跑要換掉那一節")

    def test_step_files_become_one_artifact(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-work2-")) / "notes"
        _write_note(root, "aster", "繰り返し使える観測表を一枚作る", "欄を決める",
                    "| 欄 | 意味 |\n| 日時 | 時刻 |", form="work")
        _write_note(root, "aster", "繰り返し使える観測表を一枚作る", "記入例を一行作る",
                    "| 日時 | 2026-11-07 |", form="work")
        _write_note(root, "aster", "繰り返し使える観測表を一枚作る", "使い方を三行で書く",
                    "1. 事実だけ書く", form="work")
        works = sorted((root.parent / "works" / "aster").glob("*.md"))
        self.assertEqual(len(works), 1, f"一件作品一個檔：{works}")
        text = works[0].read_text(encoding="utf-8")
        for expected in ("| 欄 | 意味 |", "| 日時 | 2026-11-07 |", "1. 事実だけ書く"):
            self.assertIn(expected, text)


class TestWorkPromptAsksForTheArtefact(unittest.TestCase):
    def test_prompt_tells_her_to_write_the_table_itself(self):
        source = pathlib.Path(__file__).resolve().parent.parent.parent.joinpath(
            "agent/brain/runtime/life_step.py").read_text(encoding="utf-8")
        self.assertIn("説明ではなく**現物**", source)
        self.assertIn("Markdown の表", source)

    def test_work_sheet_brief_requires_a_real_table(self):
        from agent.brain.runtime.life import presets_for
        brief = [preset.brief for preset in presets_for("aster") if preset.key == "work-sheet"][0]
        self.assertIn("Markdown の表", brief)
        self.assertIn("説明で済ませない", brief)


if __name__ == "__main__":
    unittest.main(verbosity=2)
