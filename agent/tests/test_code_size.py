#!/usr/bin/env python3
"""檔案／函式長度的棘輪（2026-10-25 結構 review 第 3 階段）。

拆完 api（1182）、stages（901）、tools（864）、bootstrap（859，build 628 行）之後，把現況釘住：
  ① 生產原始碼（不含 vendor/）單檔 ≤ 600 行；
  ② 單一函式 ≤ 150 行；
  ③ 還沒拆的那幾個列在白名單裡，**只准變短、不准變長**——要加東西就順手拆。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAX_FILE_LINES = 600
MAX_FUNCTION_LINES = 150

#: 還沒拆的大檔（上限＝2026-10-25 的行數）
FILE_ALLOWLIST = {
}
#: 長函式白名單已清空；新增超標函式必須直接拆，不再登记例外。
FUNCTION_ALLOWLIST = {}


def _sources():
    for folder in ("app", "agent/brain", "agent/adapter", "agent/server"):
        assert (ROOT / folder).is_dir(), f"{folder} 不見了：搬目錄要同步改這裡（否則棘輪靜默失效）"
        for path in sorted((ROOT / folder).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/vendor/" not in rel and not rel.startswith("app/tests/"):
                yield rel, path


class TestCodeSize(unittest.TestCase):
    def test_files_stay_under_the_limit(self):
        over = []
        for rel, path in _sources():
            lines = len(path.read_text(encoding="utf-8").splitlines())
            limit = FILE_ALLOWLIST.get(rel, MAX_FILE_LINES)
            if lines > limit:
                over.append(f"{rel}: {lines} 行（上限 {limit}）")
        self.assertEqual(over, [], "檔案太長：拆成套件（見 runtime/tools、compose/stages、server/api 的做法）")

    def test_functions_stay_under_the_limit(self):
        over = []
        for rel, path in _sources():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                length = node.end_lineno - node.lineno + 1
                limit = FUNCTION_ALLOWLIST.get(f"{rel}:{node.name}", MAX_FUNCTION_LINES)
                if length > limit:
                    over.append(f"{rel}:{node.lineno} {node.name}: {length} 行（上限 {limit}）")
        self.assertEqual(over, [], "函式太長：抽出有名字的步驟")

    def test_allowlist_has_no_stale_entries(self):
        # 白名單裡的檔／函式如果已經拆到上限以下，就該從白名單拿掉（棘輪往下走）
        stale = []
        for rel, limit in FILE_ALLOWLIST.items():
            path = ROOT / rel
            if not path.exists() or len(path.read_text(encoding="utf-8").splitlines()) <= MAX_FILE_LINES:
                stale.append(rel)
        self.assertEqual(stale, [], "已經不超標的檔請從 FILE_ALLOWLIST 移除")


if __name__ == "__main__":
    unittest.main(verbosity=2)
