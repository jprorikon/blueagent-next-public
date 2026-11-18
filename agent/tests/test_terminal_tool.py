#!/usr/bin/env python3
"""`terminal` 工具（owner 2026-11-03）：在她**自己的 Ubuntu VM** 里跑命令。

判據：
  ① 目標機只從 `data/vm/<entity>.json` 來——模型**不能**指定別台機器；
  ② 沒有對應 VM（檔案缺失／沒 IP）⇒ 老實回 `no_vm`，不假裝成功；
  ③ 逾時與輸出都有上限，且每一條都寫進 `_ops/shell-<entity>.jsonl`；
  ④ 只在 work 模式出現（`modes=("work",)`）。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.runtime.tools.shell import OUTPUT_CAP, TerminalTool  # noqa: E402


class _Runner:
    """假的 ssh：記下被叫了什麼，回一份可控的輸出。"""

    def __init__(self, *, code=0, text="ok") -> None:
        self.calls: list[tuple[dict, str, float]] = []
        self.code, self.text = code, text

    def __call__(self, target, cmd, timeout):
        self.calls.append((dict(target), cmd, timeout))
        return self.code, self.text


def _tool(runner, *, entity="mira", ip="192.168.122.11") -> tuple[TerminalTool, pathlib.Path]:
    root = pathlib.Path(tempfile.mkdtemp(prefix="ba-vm-"))
    (root / "vm").mkdir()
    (root / "vm" / f"{entity}.json").write_text(json.dumps({
        "entity": entity, "ip": ip, "user": entity,
        "key": f"/app/data/vm/keys/{entity}.key"}), encoding="utf-8")
    tool = TerminalTool(root / "vm", audit_root=root / "_ops", ssh=runner)
    return tool, root


class TestTerminalTool(unittest.TestCase):
    def test_runs_on_her_own_vm(self):
        runner = _Runner(code=0, text="uid=1000(mira)")
        tool, _ = _tool(runner)
        result = tool.run(SimpleNamespace(entity="mira"), {"cmd": "id"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["exit"], 0)
        target, cmd, _ = runner.calls[0]
        self.assertEqual(target["ip"], "192.168.122.11")
        self.assertEqual(cmd, "id")

    def test_no_vm_for_unknown_entity(self):
        runner = _Runner()
        tool, _ = _tool(runner)
        result = tool.run(SimpleNamespace(entity="nobody"), {"cmd": "id"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_vm")
        self.assertEqual(runner.calls, [])            # 完全沒去打 ssh

    def test_timeout_is_clamped(self):
        runner = _Runner()
        tool, _ = _tool(runner)
        tool.run(SimpleNamespace(entity="mira"), {"cmd": "sleep 100", "timeout_s": 999})
        self.assertLessEqual(runner.calls[0][2], 60.0)

    def test_output_is_capped(self):
        runner = _Runner(code=0, text="あ" * (OUTPUT_CAP))
        tool, _ = _tool(runner)
        result = tool.run(SimpleNamespace(entity="mira"), {"cmd": "yes"})
        self.assertTrue(result.data["truncated"])
        self.assertLessEqual(len(result.data["out"].encode("utf-8")), OUTPUT_CAP + 64)

    def test_nonzero_exit_is_reported(self):
        runner = _Runner(code=2, text="bash: nope: not found")
        tool, _ = _tool(runner)
        result = tool.run(SimpleNamespace(entity="mira"), {"cmd": "nope"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "exit_2")

    def test_audit_line_is_written(self):
        runner = _Runner()
        tool, root = _tool(runner)
        tool.run(SimpleNamespace(entity="mira"), {"cmd": "ls -la"})
        lines = (root / "_ops" / "shell-mira.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["cmd"], "ls -la")
        self.assertEqual(row["entity"], "mira")
        self.assertIn("ms", row)

    def test_modes_only_work(self):
        self.assertEqual(TerminalTool.modes, ("work",))


if __name__ == "__main__":
    unittest.main(verbosity=2)
