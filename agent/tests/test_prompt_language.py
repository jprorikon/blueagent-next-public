"""守門：**會進 prompt 的文案必須是日語**（owner 2026-10-27「清除掉基础 prompt 中的非日语」）。

判據與字串提取在 `tests/prompt_lang.py`（與渲染級測試共用）：
  · 用 `tokenize` 取**完整**字串常量——多行模板（`_DEFAULT`、`PROMPT`）也看得到；
  · 字表分兩組：`STRICT`（日文不用的簡／繁字形，一個就算）與 `LOOSE`（的・了・我・什，兩個才算）；
  · docstring、日誌呼叫的引數、帶 `# lang-ok` 註解的語句（比對詞表、存檔鍵）不算。

2026-10-28 review 指出舊版三處漏檢：清單沒收 decide／distill／rerank／summary、只取單行字串、
字表只有簡體且門檻 ≥2——繁中段落與短字串都放過了。這一支只是靜態那一層；
真的合成路徑另外在 `test_prompt_render_language.py`／各模組測試裡做渲染級斷言。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "agent" / "tests"))

from prompt_lang import chinese_hits, string_constants  # noqa: E402

PROMPT_FILES = [
    "agent/brain/messages.py",
    "agent/brain/compose/stages/persona.py",
    "agent/brain/runtime/inner/timeframe.py",
    "agent/brain/runtime/skills.py",
    "agent/brain/persona/userfile.py",
    "agent/brain/runtime/tools/base.py",
    "agent/brain/runtime/tools/delegate.py",
    "agent/brain/runtime/tools/memory.py",
    "agent/brain/runtime/tools/playground.py",
    "agent/brain/runtime/tools/records.py",
    "agent/brain/runtime/tools/remind.py",
    "agent/brain/runtime/tools/skills.py",
    "agent/brain/runtime/tools/voice.py",
    # 她的「內心工作」prompt（owner 2026-10-27 第二批：三人以日語為母語）
    "agent/brain/runtime/life.py",
    "agent/brain/runtime/life_step.py",
    "brain/runtime/life_presets.py",
    "agent/brain/runtime/self_work.py",
    "agent/brain/runtime/prethink.py",
    "agent/brain/runtime/subagents.py",
    "agent/brain/runtime/delegate.py",
    "agent/brain/runtime/reflection.py",
    "agent/brain/compose/heartbeat_compose.py",
    "agent/brain/compose/stance_seed.py",
    "agent/adapter/trpg/prompt.py",
    "agent/adapter/trpg/report.py",
    "agent/adapter/trpg/mood.py",
    "agent/adapter/trpg/seat.py",
    "agent/adapter/stt_local.py",
    "agent/brain/runtime/stance.py",
    "agent/brain/domain/stance.py",
    # review 2026-10-28：以下幾支以前不在清單裡（牌面／提煉／重排／承接摘要），整段中文照樣通過
    "agent/adapter/trpg/decide.py",
    "agent/brain/runtime/distill.py",
    "agent/brain/runtime/memory/rerank.py",
    "agent/brain/compose/summary.py",
]



class TestPromptIsJapanese(unittest.TestCase):
    def test_no_chinese_in_dialogue_prompt(self):
        offenders = []
        for rel in PROMPT_FILES:
            path = ROOT / rel
            if not path.exists():
                continue
            for lineno, text in string_constants(path):
                for hit in chinese_hits(text):
                    offenders.append(f"{rel}:{lineno} {hit[:70]}")
        self.assertEqual(offenders, [],
                         "prompt 裡還有中文（要日語；確實不進 prompt 的資料加 `# lang-ok: 理由`）：\n"
                         + "\n".join(offenders))

    def test_skill_files_are_japanese(self):
        """技能檔（`brain/skills/*/SKILL.md`）整份都會進模型：description 進技能索引，正文經
        `skill.view` 回到工具結果。2026-10-31 複檢：七支全是中文、這支測試只掃 `.py` 所以沒擋住。"""
        offenders = []
        for path in sorted((ROOT / "agent" / "brain" / "skills").glob("*/SKILL.md")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for hit in chinese_hits(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno} {hit[:70]}")
        self.assertEqual(offenders, [], "技能檔裡還有中文（要日語）：\n" + "\n".join(offenders))

    def test_detector_sees_multiline_templates_and_short_strings(self):
        """偵測器自己的回歸：舊版會漏的三種形狀，現在都要抓得到。"""
        import tempfile
        sample = (
            'TEMPLATE = """標題\n（还没有）\n"""\n'          # 多行模板
            'ERR = "她"\n'                                     # 短字串
            'MIXED = (\n    "這條線："\n    f"{1}"\n)\n'       # 續行裡的繁體
            'OK = "比對用"  # lang-ok: 詞表\n'
            'print("這不進 prompt")\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "sample.py"
            path.write_text(sample, encoding="utf-8")
            found = [text for _line, text in string_constants(path) if chinese_hits(text)]
        self.assertEqual(len(found), 3, found)


if __name__ == "__main__":
    unittest.main(verbosity=2)
