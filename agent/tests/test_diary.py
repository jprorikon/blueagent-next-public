"""她的日記與工作成果（`/api/diary`；owner 2026-10-31）：讀得到、私記不給、路徑走不出去、只有主人能看。"""

from __future__ import annotations

import datetime
import os
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose import diary
from agent.brain.domain.auth import AuthContext, Role
from agent.server import api

TODAY = datetime.date(2026, 10, 31)


def seed(base: pathlib.Path) -> None:
    notes = base / "notes" / "mira"
    works = base / "works" / "mira"
    notes.mkdir(parents=True, exist_ok=True)      # 同一個 data 目錄可能被別的測試類播過
    works.mkdir(parents=True, exist_ok=True)
    (notes / "2026-10-31.md").write_text(
        "\n## 09:10 「朝の散歩」／公園を一周\n池にカモがいた。\n"
        "\n## 10:00 「秘密の練習」／歌　〔private〕\nだれにも言わない。\n"
        "\n## 11:30 「小さな作品」／詩を書く　〔作品：2026-10-31-1130-0-poem.md〕\n秋の詩を書いた。\n",
        encoding="utf-8")
    (notes / "reflection-2026-10-31.md").write_text("# 2026-10-31 日記\n\n今日は散歩して詩を書いた。\n",
                                                    encoding="utf-8")
    (notes / "reflection-2026-10-29.md").write_text("# 2026-10-29 日記\n\n雨だった。\n", encoding="utf-8")
    (works / "2026-10-31-1130-0-poem.md").write_text("# 小さな作品\n\n## 詩を書く\n\n秋の風\n", encoding="utf-8")


class TestDiaryReader(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="ba-diary-"))
        seed(self.base)

    def test_days_notes_and_reflection(self):
        out = diary.diary(self.base, "mira", days=7, today=TODAY)
        self.assertTrue(out["ok"])
        self.assertEqual([day["date"] for day in out["days"]], ["2026-10-31", "2026-10-29"])
        today = out["days"][0]
        self.assertEqual(today["reflection"], "今日は散歩して詩を書いた。")
        self.assertEqual(today["private_hidden"], 1)
        # 2026-10-31：索引**不帶正文**（一天上千步 ⇒ 一次全帶會把 App 卡死），只交代「那天有幾步」
        self.assertEqual(today["notes_total"], 2)
        self.assertNotIn("notes", today)
        day = diary.day(self.base, "mira", "2026-10-31")
        self.assertEqual([note["activity"] for note in day["notes"]], ["小さな作品", "朝の散歩"])  # 新的在前
        self.assertEqual(day["notes"][1]["time"], "09:10")
        self.assertEqual(day["notes"][1]["step"], "公園を一周")
        self.assertEqual(day["notes"][0]["work"], "2026-10-31-1130-0-poem.md")
        self.assertEqual(day["notes_total"], 2)
        self.assertEqual(day["private_hidden"], 1)
        self.assertNotIn("だれにも言わない", str(out))          # 私記不回流
        self.assertNotIn("だれにも言わない", str(day))
        self.assertEqual(out["works"][0]["title"], "小さな作品")

    def test_notes_are_capped_per_day(self):
        """一天上千步時只回最近 30 步、正文截短（回多少筆＋notes_total 說全部有幾步）。"""
        path = self.base / "notes" / "mira" / "2026-10-30.md"
        blocks = ["\n## %02d:%02d 「散歩」／%d 周目\n%s" % (h, m, i, "あ" * 500)
                  for i, (h, m) in enumerate((h, m) for h in range(6, 24) for m in (0, 10, 20, 30, 40, 50))]
        path.write_text("".join(blocks), encoding="utf-8")
        day = diary.day(self.base, "mira", "2026-10-30")
        self.assertEqual(len(day["notes"]), diary.MAX_NOTES_PER_DAY)
        self.assertEqual(day["notes_total"], len(blocks))
        self.assertEqual(day["notes"][0]["time"], "23:50")      # 最近的那一步在最前面
        self.assertLessEqual(len(day["notes"][0]["body"]), diary.PREVIEW_BODY)

    def test_works_newest_first_by_mtime(self):
        """作品檔名不帶時分了（同名覆蓋）⇒ 清單要照修改時間排，不能照檔名倒序。"""
        folder = self.base / "works" / "mira"
        older = folder / "2026-10-31-zzz-old.md"
        newer = folder / "2026-10-31-aaa-new.md"
        older.write_text("# 古い\n", encoding="utf-8")
        newer.write_text("# 新しい\n", encoding="utf-8")
        os.utime(older, (1_000_000_000, 1_000_000_000))
        names = [row["name"] for row in diary.works(self.base, "mira")]
        self.assertLess(names.index(newer.name), names.index(older.name), names)

    def test_index_summary_follows_file_changes(self):
        """索引的「那天有幾步」有快取，但檔一變（多寫一步）就要跟著變。"""
        path = self.base / "notes" / "mira" / "2026-10-31.md"
        before = diary.diary(self.base, "mira", days=1, today=TODAY)["days"][0]["notes_total"]
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n## 12:00 「昼」／ひと休み\nお茶。\n")
        after = diary.diary(self.base, "mira", days=1, today=TODAY)["days"][0]["notes_total"]
        self.assertEqual(after, before + 1)

    def test_day_rejects_bad_date(self):
        for bad in ("", "2026-13-99", "../2026-10-31", "2026-10-31.md"):
            self.assertIsNone(diary.day(self.base, "mira", bad), bad)

    def test_work_full_text_and_no_traversal(self):
        self.assertIn("秋の風", diary.work(self.base, "mira", "2026-10-31-1130-0-poem.md")["text"])
        for bad in ("../notes/mira/2026-10-31.md", "..\\x.md", "nope.md", "2026-10-31.md", ""):
            self.assertIsNone(diary.work(self.base, "mira", bad), bad)
        self.assertIsNone(diary.work(self.base, "../mira", "2026-10-31-1130-0-poem.md"))
        self.assertFalse(diary.diary(self.base, "../etc")["ok"])

    def test_empty_entity_folder(self):
        out = diary.diary(self.base, "aster", today=TODAY)
        self.assertEqual((out["days"], out["works"]), ([], []))


class TestDiaryEndpoint(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        seed(pathlib.Path(self.state.app.data_dir))
        self.owner = self.state.app.auth.owner_context()

    def test_owner_only(self):
        guest = AuthContext("guest-a", Role.GUEST, frozenset({"chat"}))
        status, _ = api.get_diary(self.state, {"entity": ["mira"]}, auth=guest)
        self.assertEqual(status, 403)
        status, _ = api.get_diary(self.state, {"entity": ["mira"]}, auth=None)
        self.assertIn(status, (401, 403))

    def test_owner_reads_diary_and_work(self):
        status, body = api.get_diary(self.state, {"entity": ["mira"], "days": ["31"]}, auth=self.owner)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["works"])
        self.assertIn("digest", body)                          # 端末拿它問 304
        status, day = api.get_diary_day(self.state, {"entity": ["mira"], "date": ["2026-10-31"]},
                                        auth=self.owner)
        self.assertEqual(status, 200, day)
        self.assertEqual(day["date"], "2026-10-31")
        status, _ = api.get_diary_day(self.state, {"entity": ["mira"], "date": ["nope"]}, auth=self.owner)
        self.assertEqual(status, 404)
        status, work = api.get_diary_work(self.state, {"entity": ["mira"],
                                                        "name": ["2026-10-31-1130-0-poem.md"]}, auth=self.owner)
        self.assertEqual(status, 200, work)
        status, _ = api.get_diary(self.state, {"entity": ["nobody"]}, auth=self.owner)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
