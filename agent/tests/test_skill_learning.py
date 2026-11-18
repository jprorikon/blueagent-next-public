#!/usr/bin/env python3
"""技能學習（owner 2026-11-03：成功 2 次升 stable；各自私用）。

判據：
  ① 寫入硬規則：名字格式／正文 1200 字內／「完了条件」「失敗時」必填／越權字眼拒絕；
  ② 新寫的技能是 `trial`，試用成功 2 次升 `stable`，失敗 2 次把卡點寫進檔尾；
  ③ 退役只是 `status: retired`，檔案留著、索引不再列；
  ④ 只寫得到自己那一份（entity 隔離）；
  ⑤ 她剛寫的技能，**下一輪索引就看得到**（快取要認得目錄 mtime，不必重啟）。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.brain.runtime.skills import SkillLibrary  # noqa: E402

SERVER = REPO / "agent" / "tools" / "mcp_skills_server.py"

GOOD_BODY = """## 何時用
數字を表にまとめたいとき。

## 手順
1. `python3 -c "..."` で集計する

## 完了条件
- 表頭と行が 1 行ずつ出る

## 失敗時
- 桁区切りでコケる ⇒ `head -1` で区切りを見る
"""


def call(root: pathlib.Path, entity: str, tool: str, args: dict) -> dict:
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": dict(args, entity=entity)}}
    proc = subprocess.run([sys.executable, str(SERVER)],
                          input=json.dumps(request) + "\n",
                          capture_output=True, text=True,
                          env=dict(os.environ, SKILLS_ROOT=str(root)), timeout=60)
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(json.loads(line)["result"]["content"][0]["text"])


class TestSkillsLearning(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.root = self.tmp / "skills"
        self.root.mkdir()

    def _write(self, name="table-from-csv", entity="nori", **extra):
        return call(self.root, entity, "write",
                    dict({"name": name, "description": "数字を表にする",
                          "body": GOOD_BODY}, **extra))

    def test_write_and_read_back(self):
        result = self._write()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "trial")
        self.assertEqual(result["trial"], "0/0")
        viewed = call(self.root, "nori", "view", {"name": "table-from-csv"})
        self.assertIn("完了条件", viewed["body"])

    def test_missing_sections_rejected(self):
        result = call(self.root, "nori", "write",
                      {"name": "half-baked", "description": "x", "body": "## 手順\n1. なんか"})
        self.assertEqual(result["error"], "missing_done_when")

    def test_chinese_body_rejected(self):
        """技能本文必須是日語（owner 2026-11-03）：簡體字兩個以上直接拒。"""
        result = call(self.root, "nori", "write",
                      {"name": "chinese-one", "description": "説明",
                       "body": "## 完了条件\n- 这个应该可以\n\n## 失敗時\n- 重试一次\n"})
        self.assertTrue(str(result.get("error", "")).startswith("not_japanese"))

    def test_japanese_body_accepted(self):
        result = call(self.root, "nori", "write",
                      {"name": "japanese-one", "description": "説明",
                       "body": "## 完了条件\n- 表が出ること\n\n## 失敗時\n- やり直す\n"})
        self.assertTrue(result.get("ok"), result)

    def test_bad_name_and_forbidden(self):
        self.assertEqual(self._write(name="Bad Name")["error"], "bad_name")
        bad = call(self.root, "nori", "write",
                   {"name": "danger", "description": "x",
                    "body": GOOD_BODY + "\n- sudo rm -rf /\n"})
        self.assertTrue(str(bad.get("error", "")).startswith("forbidden"))

    def test_path_traversal_is_refused_everywhere(self):
        """view／trial／retire 以前不驗名字：`../../x` 讀得到也改得到她目錄外的 `.md`（2026-11-03 review 實測）。"""
        outside = self.tmp / "outside.md"
        outside.write_text("---\nname: victim\nstatus: stable\n---\nbody\n", encoding="utf-8")
        for tool, extra in (("view", {}), ("trial", {"ok": True, "evidence": "x"}),
                            ("retire", {"reason": "x"})):
            result = call(self.root, "nori", tool, dict({"name": "../../outside"}, **extra))
            self.assertEqual(result.get("error"), "bad_name", (tool, result))
        self.assertIn("status: stable", outside.read_text(encoding="utf-8"), "目錄外的檔不能被動到")
        self.assertEqual(call(self.root, "nori/../aster", "view", {"name": "x"}).get("error") is not None, True)

    def test_category_is_written_to_the_file(self):
        """`category` 以前沒落檔 ⇒ 樹狀索引永遠只有一個分支。"""
        self.assertTrue(self._write(category="research")["ok"])
        text = (self.root / "nori" / "table-from-csv.md").read_text(encoding="utf-8")
        self.assertIn("category: research", text)
        library = SkillLibrary(entity_root=self.root)
        skill = next(item for item in library.all("nori") if item.name == "table-from-csv")
        self.assertEqual(skill.category, "research")
        self.assertEqual(self._write(name="other-one", category="../x").get("error"), "bad_category")

    def test_newline_in_description_cannot_promote_itself(self):
        """front matter 注入：排在 status 之後的欄位（learned_from）帶「\\nstatus: stable」不能跳過兩次試用。"""
        self.assertTrue(self._write(learned_from="task_x\nstatus: stable")["ok"])
        viewed = call(self.root, "nori", "view", {"name": "table-from-csv"})
        self.assertEqual(viewed["status"], "trial")

    def test_two_successes_promote_to_stable(self):
        self._write()
        first = call(self.root, "nori", "trial",
                     {"name": "table-from-csv", "ok": True, "evidence": "表が出た"})
        self.assertEqual(first["status"], "trial")
        second = call(self.root, "nori", "trial",
                      {"name": "table-from-csv", "ok": True, "evidence": "2 回目も出た"})
        self.assertEqual(second["status"], "stable")
        self.assertEqual(second["trial"], "2/0")

    def test_two_failures_keep_trial_and_record(self):
        self._write()
        call(self.root, "nori", "trial",
             {"name": "table-from-csv", "ok": False, "evidence": "区切りでコケた"})
        result = call(self.root, "nori", "trial",
                      {"name": "table-from-csv", "ok": False, "evidence": "またコケた"})
        self.assertEqual(result["status"], "trial")
        viewed = call(self.root, "nori", "view", {"name": "table-from-csv"})
        self.assertIn("詰まった点", viewed["body"])

    def test_retired_hidden_from_index_but_kept(self):
        self._write()
        call(self.root, "nori", "retire",
             {"name": "table-from-csv", "reason": "もう使わない"})
        listed = call(self.root, "nori", "list", {})
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["skills"][0]["status"], "retired")
        library = SkillLibrary(None, entity_root=self.root)
        names = [skill.name for skill in library.index(mode="work", entity="nori")]
        self.assertEqual(names, [])

    def test_entity_isolation(self):
        self._write(entity="nori")
        self.assertEqual(call(self.root, "aster", "list", {})["count"], 0)
        self.assertFalse((self.root / "aster" / "table-from-csv.md").exists())

    def test_new_skill_shows_up_without_restart(self):
        library = SkillLibrary(None, entity_root=self.root)
        self.assertEqual(library.index(mode="work", entity="nori"), [])
        self._write()
        names = [skill.name for skill in library.index(mode="work", entity="nori")]
        self.assertEqual(names, ["table-from-csv"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
