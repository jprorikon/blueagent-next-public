#!/usr/bin/env python3
"""複習自測 ＋ 做小作品（owner 2026-10-24 選的兩項）。

判據：

  ① **複習自測**要用得到真材料：`notes.read` 讀得到自己筆記的正文，
     別人的筆記讀不到，標了 `〔private〕` 的段落不回流；
  ② **做小作品**要真的落成一份**成品檔**（`data/works/<entity>/…md`），
     不是只埋在日記流裡的一段；日記裡留一行索引指到成品；
  ③ 每套活動自己的規格（`brief`）要進到提示詞，不能被「2～4 句」那條蓋掉。
"""

from __future__ import annotations

import datetime
import pathlib
import tempfile

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.life import build_activity, make_step_fn, presets_for
from agent.brain.memory.in_memory import InMemoryMemory
from agent.brain.domain.activity import Activity
from agent.brain.domain.entity import CharacterEntity, EntityRegistry
from agent.brain.runtime.activity_log import PRIVATE, PUBLIC, ActivityLog
from agent.brain.runtime.self_work import run_step
from agent.brain.runtime.tools import NotesReadTool, ToolContext, ToolRegistry


def _registry() -> EntityRegistry:
    entities = EntityRegistry()
    for ident in ("mira", "aster", "nori"):
        entities.register(CharacterEntity(id=ident, name=ident))
    return entities


class TestNotesRead(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-notes-"))
        notes = self.root / "notes"
        (notes / "mira").mkdir(parents=True)
        (notes / "aster").mkdir(parents=True)
        # `notes.read` 只讀最近幾天的當天筆記檔 ⇒ 用今天的日期（寫死日期過兩天就讀不到）
        today = datetime.date.today().isoformat()
        (notes / "mira" / f"{today}.md").write_text(
            f"# {today}\n\n## 10:00 「學判定」／讀規則書\n"
            "プッシュロールは失敗を代償つきで振り直せる。\n\n"
            "## 11:00 「學判定」／有點累\n　〔private〕\n本当はよく分かっていない。\n",
            encoding="utf-8")
        (notes / "aster" / f"{today}.md").write_text(
            f"# {today}\n\n## 09:00 「觀測」／讀記錄\nアスターだけの記録。\n",
            encoding="utf-8")
        self.tool = NotesReadTool(notes)

    def _ctx(self, entity: str) -> ToolContext:
        return ToolContext(entity=entity, principal="owner", thread_id="self",
                           source="self", voice=None, mode="work")

    def test_reads_own_notes(self):
        result = self.tool.run(self._ctx("mira"), {"days": 3, "limit": 6})
        self.assertTrue(result.ok, result.error)
        self.assertIn("プッシュロール", result.data["summary"])

    def test_private_sections_are_hidden(self):
        result = self.tool.run(self._ctx("mira"), {"days": 3, "limit": 6})
        self.assertNotIn("よく分かっていない", result.data["summary"],
                         "private 段落不能回流（她會在公開產出裡引用）")

    def test_cannot_read_someone_elses_notes(self):
        result = self.tool.run(self._ctx("mira"), {"entity": "aster"})
        self.assertNotIn("アスターだけ", result.data["summary"], "只讀自己的")

    def test_empty_notes_is_not_an_error(self):
        result = self.tool.run(self._ctx("nori"), {})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["count"], 0)


class TestWorkDeliverable(unittest.TestCase):
    def _run(self, preset_key: str, entity: str = "mira"):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-work-"))
        log = ActivityLog(root / "logs")
        write = make_step_fn(InMemoryMemory(), _registry(), log=log,
                             notes_root=root / "notes",
                             call=lambda prompt: "開場：雨停在窗上，桌上的骰子還在轉。")
        library = {item.key: item for item in presets_for(entity)}
        preset = library[preset_key]
        activity = Activity.create(entity, preset.title, [text for text, _ in preset.steps],
                                   kind=preset.kind,
                                   visibilities=[vis for _, vis in preset.steps])
        activity.meta.update({"preset": preset.key, "form": preset.form,
                              "brief": preset.brief})
        write(activity, activity.steps[0])
        return root, activity, preset

    def test_work_preset_writes_a_deliverable_file(self):
        root, activity, preset = self._run("work-scenario")
        works = list((root / "works" / "mira").glob("*.md"))
        self.assertEqual(len(works), 1, works)
        text = works[0].read_text(encoding="utf-8")
        self.assertIn(activity.title, text)
        self.assertIn("骰子", text)
        # 日記裡要留一行索引指到成品
        note = next((root / "notes" / "mira").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("〔作品：", note)
        self.assertIn(works[0].name, note)

    def test_note_preset_does_not_write_a_deliverable(self):
        root, _, _ = self._run("review-quiz")
        self.assertFalse((root / "works").exists(), "記事型活動不該產生成品檔")

    def test_presets_declare_form(self):
        for entity in ("mira", "aster", "nori"):
            forms = {item.key: item.form for item in presets_for(entity)}
            self.assertIn("review-quiz", forms, entity)
            self.assertEqual(forms["review-quiz"], "note")
            works = [key for key, form in forms.items() if form == "work"]
            self.assertTrue(works, f"{entity} 應該有一套『做小作品』")
            self.assertTrue(all(item.brief for item in presets_for(entity)
                                if item.form == "work"), "成品要寫規格（長度／格式）")


class TestReviewPrefetchesHerNotes(unittest.TestCase):
    """複習／自測那一步要**先把她自己的筆記抓進 prompt**（模型不會自己去叫 notes.read）。"""

    def test_notes_are_injected_before_the_model_answers(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-review-"))
        notes = root / "notes" / "mira"
        notes.mkdir(parents=True)
        today = datetime.date.today().isoformat()
        (notes / f"{today}.md").write_text(
            f"# {today}\n\n## 10:00 「學判定」／讀規則書\n"
            "プッシュロールは代償つきで振り直せる。\n", encoding="utf-8")
        tools = ToolRegistry().register(NotesReadTool(root / "notes"))
        prompts: list[str] = []

        class _Brain:
            def run_turn(self, request):
                prompts.append(str(getattr(request, "text", "") or ""))
                yield ("done", {"text": "三道題：……"})

        result = run_step(_Brain(), tools, entity="mira",
                          activity="複習自己的筆記，自己出題考自己",
                          step="讀自己這幾天的筆記，挑兩條能考的",
                          context_factory=lambda: ToolContext(
                              entity="mira", principal="owner", thread_id="self",
                              source="self", voice=None, mode="work"))
        self.assertTrue(result["ok"], result)
        self.assertIn("プッシュロール", prompts[0], "筆記本體要先出現在 prompt 裡")
        self.assertEqual(result["trace"][0]["name"], "notes.read")
        self.assertTrue(result["trace"][0]["ok"])

    def test_missing_notes_tells_her_not_to_invent(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-review2-"))
        tools = ToolRegistry().register(NotesReadTool(root / "notes"))
        prompts: list[str] = []

        class _Brain:
            def run_turn(self, request):
                prompts.append(str(getattr(request, "text", "") or ""))
                yield ("done", {"text": "沒有材料。"})

        run_step(_Brain(), tools, entity="mira", activity="複習",
                 step="讀自己的筆記",
                 context_factory=lambda: ToolContext(
                     entity="mira", principal="owner", thread_id="self",
                     source="self", voice=None, mode="work"))
        self.assertIn("ノートの中身を作らない", prompts[0])


class TestBriefReachesPrompt(unittest.TestCase):
    def test_artefact_round_gets_a_bigger_token_budget(self):
        """做作品那一輪要放寬 tokens（全站 400 只夠 2～4 句，寫不完 300 字）。"""
        from agent.brain.runtime.self_work import ARTEFACT_MAX_TOKENS
        seen: list[dict] = []

        class _Brain:
            def run_turn(self, request):
                seen.append(dict(getattr(request, "meta", {}) or {}))
                yield ("done", {"text": "成品"})

        run_step(_Brain(), ToolRegistry(), entity="mira", activity="寫場景",
                 step="寫成 300 字", form="work", brief="300 字",
                 context_factory=lambda: ToolContext(entity="mira", principal="owner",
                                                     thread_id="self", source="self",
                                                     voice=None, mode="work"))
        self.assertEqual(seen[0].get("max_tokens"), ARTEFACT_MAX_TOKENS)

    def test_brief_is_appended_and_overrides_short_rule(self):
        prompts: list[str] = []

        class _Brain:
            def run_turn(self, request):
                prompts.append(str(getattr(request, "text", "") or ""))
                yield ("done", {"text": "できた。"})

        result = run_step(_Brain(), ToolRegistry(), entity="mira",
                          activity="寫一小段能上桌的場景", step="把開場寫成 300 字左右的成品",
                          brief="要 300 字、有具體場所與物件",
                          context_factory=lambda: ToolContext(
                              entity="mira", principal="owner", thread_id="self",
                              source="self", voice=None, mode="work"))
        self.assertTrue(result["ok"], result)
        self.assertIn("300 字", prompts[0])
        self.assertIn("上の「2〜4文」より優先", prompts[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
