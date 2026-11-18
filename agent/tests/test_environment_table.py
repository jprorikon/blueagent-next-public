#!/usr/bin/env python3
"""環境變數總表必須跟程式同步，並覆蓋直接、常數與動態讀取。"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent" / "tools"))

import gen_env_table as env  # noqa: E402


class TestEnvironmentTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = env.scan()

    def test_generated_table_is_current(self):
        self.assertEqual(env.DOC.read_text(encoding="utf-8"), env.render(self.rows),
                         "環境變數總表過期：python3 agent/tools/gen_env_table.py 重產")

    def test_direct_indirect_and_dynamic_reads_are_covered(self):
        self.assertIn("BLUEAGENT_DATA_DIR", self.rows)
        self.assertIn("BLUEAGENT_BUDGET_NET_COST", self.rows)  # 常數名間接讀取
        self.assertIn("BLUEAGENT_BUDGET_PER_TICK", self.rows)  # helper 參數間接讀取
        self.assertIn("BLUEAGENT_LLM_<ROLE>_MODEL", self.rows)
        self.assertIn("BLUEAGENT_HEARTBEAT_S_<ENTITY>", self.rows)

    def test_every_row_has_a_source(self):
        self.assertTrue(self.rows)
        self.assertEqual([name for name, row in self.rows.items() if not row["files"]], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
