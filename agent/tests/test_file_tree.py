#!/usr/bin/env python3
"""守門：`docs/FILE-TREE.md` 列的路徑跟倉庫一致（2026-10-28 建立檔案樹時加）。

只比「有哪些路徑」：新增／刪除／搬了模組卻沒重產（`python3 agent/tools/gen_file_tree.py`）就紅；
改 docstring 不紅（說明文字取自原檔，重產時才更新）。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent" / "tools"))

import gen_file_tree as tree  # noqa: E402


class TestFileTree(unittest.TestCase):
    def test_tree_lists_exactly_the_tracked_paths(self):
        files = tree.tracked()
        want = tree.expected_paths(files)
        listed = tree.listed_paths(tree.DOC.read_text(encoding="utf-8"))
        missing = sorted(want - listed)
        extra = sorted(path for path in listed - want if path.endswith((".py", ".cs", ".md")))
        self.assertEqual((missing, extra), ([], []),
                         "檔案樹過期了：python3 agent/tools/gen_file_tree.py 重產")

    def test_parser_round_trips_and_catches_drift(self):
        """產物反推得回它列的每一條；少一個模組就抓得到。"""
        files = tree.tracked()
        text = tree.BEGIN + "\n" + tree.render(files) + "\n" + tree.END
        want = tree.expected_paths(files)
        self.assertEqual(want - tree.listed_paths(text), set())
        dropped = "\n".join(line for line in text.splitlines()
                            if not line.strip().startswith("memory_search.py"))
        self.assertIn("agent/brain/runtime/memory/memory_search.py", want - tree.listed_paths(dropped))


if __name__ == "__main__":
    unittest.main(verbosity=2)
