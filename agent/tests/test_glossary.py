#!/usr/bin/env python3
"""術語表（`docs/GLOSSARY.md`）的守門：詞只能有一份定義，註解不准引用不存在的詞。

判據（設計借自 `Sallyn0225/eidolon-sc` 的 GLOSSARY，owner 2026-11-01）：

  ① 每條術語都要有定義，而且要有 `_Avoid_`（不要叫什麼）——沒有邊界的詞等於沒定義；
  ② 同一條術語只能定義一次（兩個地方各寫一份，遲早一個改了一個沒改）；
  ③ 代碼與文件裡寫 `GLOSSARY.md「某詞」` 的，那個詞**必須真的在表裡**
     （不然註解指到空氣，讀的人以為有定義）；
  ④ 表裡至少要有一條——防止有人把整份檔清空而測試還是綠的。

為什麼要進測試：術語漂移是靜默的。同一個東西叫「回合」又叫「輪次」，半年後
prompt、看板、測試各說各話，而且没有任何一行代碼會報錯。
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GLOSSARY = ROOT / "docs" / "GLOSSARY.md"
#: 註解裡引用術語的寫法：GLOSSARY.md「回合」
CITATION = re.compile(r"GLOSSARY\.md「([^」]{1,20})」")
ENTRY = re.compile(r"^\*\*(.+?)\s*\(([^)]+)\)\*\*\s*:\s*$", re.MULTILINE)


def entries(text: str) -> dict[str, str]:
    """術語 → 那一條的正文（到下一條為止）。"""
    found: dict[str, str] = {}
    matches = list(ENTRY.finditer(text))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        found.setdefault(match.group(1).strip(), text[start:end])
    return found


def sources():
    for folder in ("brain", "adapter", "server"):
        for path in sorted((ROOT / "agent" / folder).rglob("*.py")):
            if "/vendor/" in path.as_posix():
                continue
            yield path
    for path in sorted((ROOT / "docs").glob("*.md")):
        yield path


class TestGlossary(unittest.TestCase):
    def setUp(self):
        self.text = GLOSSARY.read_text(encoding="utf-8")
        self.table = entries(self.text)

    def test_glossary_exists_and_has_entries(self):
        self.assertTrue(GLOSSARY.exists(), "docs/GLOSSARY.md 不見了")
        self.assertGreaterEqual(len(self.table), 8, f"條目太少：{sorted(self.table)}")

    def test_every_entry_has_a_definition_and_avoid_line(self):
        for term, body in self.table.items():
            self.assertGreaterEqual(len(body.strip()), 30, f"「{term}」沒有定義")
            self.assertIn("_Avoid_", body, f"「{term}」沒寫不要叫什麼（_Avoid_）")

    def test_no_term_is_defined_twice(self):
        names = ENTRY.findall(self.text)
        seen = [name for name, _ in names]
        duplicated = {name for name in seen if seen.count(name) > 1}
        self.assertEqual(duplicated, set(), f"同一條定義了兩次：{duplicated}")

    def test_citations_point_at_real_terms(self):
        missing: list[str] = []
        for path in sources():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for term in CITATION.findall(text):
                if term not in self.table and term not in missing:
                    missing.append(term)
        self.assertEqual(missing, [], f"這些引用在術語表裡找不到：{missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
