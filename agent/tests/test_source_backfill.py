"""歷史來源清洗（`runtime/source_backfill.py`）的驗收。

判據（與 SKILL 說明一致）：
  ① `raw/` 說群聊（scene=discord-lobby）而 outbox 標 phone ⇒ **改**；
  ② outbox 空標籤、raw 有明確 source ⇒ **補**；
  ③ `scene=together` + phone ⇒ 舊私訊與 App 打字同形 ⇒ **只報不改**（owner 點名才改）；
  ④ 寫入前自動備份。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))   # 倉庫根

from agent.brain.runtime.source_backfill import apply, plan  # noqa: E402


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class BackfillSourceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _outbox(self, rows):
        _write_jsonl(self.data / "outbox" / "index.jsonl", rows)

    def _raw(self, rows):
        _write_jsonl(self.data / "raw" / "mira" / "2026-10.jsonl", rows)

    # ── ① 群聊被標成 phone ⇒ 改成 discord ──
    def test_group_row_mislabelled_as_phone_is_rewritten(self):
        self._outbox([{"turn_id": "t1", "at": 100.0, "entity": "mira",
                       "source": "phone:owner", "asked": "在吗"}])
        self._raw([{"at": 99.0, "entity": "mira", "role": "user",
                    "speaker": "owner", "source": "discord:owner",
                    "scene": "discord-lobby", "text": "在吗"}])
        result = apply(self.data, dry_run=True)
        self.assertEqual(result["rewrite_count"], 1)
        self.assertEqual(result["rewrites"][0]["new"], "discord:owner")

    # ── ② 空標籤 ⇒ 補 discord 類（不影響 App 可見性）──
    def test_empty_source_is_filled_with_discord_label(self):
        self._outbox([{"turn_id": "t2", "at": 100.0, "entity": "mira",
                       "source": "", "asked": "こんにちは"}])
        self._raw([{"at": 99.0, "entity": "mira", "role": "user",
                    "speaker": "owner", "source": "discord:dm:owner",
                    "scene": "together", "text": "こんにちは"}])
        result = apply(self.data, dry_run=True)
        self.assertEqual(result["rewrite_count"], 1)
        self.assertEqual(result["rewrites"][0]["new"], "discord:dm:owner")

    # ── ②b 空標籤 + raw 是 phone ⇒ **不補**（補了會讓它新出現在 App 歷史）──
    def test_empty_source_with_phone_label_is_not_filled(self):
        self._outbox([{"turn_id": "t2b", "at": 100.0, "entity": "mira",
                       "source": "", "asked": "拉最新代码"}])
        self._raw([{"at": 99.0, "entity": "mira", "role": "user",
                    "speaker": "owner", "source": "phone:owner",
                    "scene": "together", "text": "拉最新代码"}])
        result = apply(self.data, dry_run=True)
        self.assertEqual(result["rewrite_count"], 0)
        self.assertEqual(result["stats"].get("empty_would_reveal"), 1)

    # ── ③ 同形 ⇒ 只報不改 ──
    def test_together_phone_is_reported_not_changed(self):
        self._outbox([{"turn_id": "t3", "at": 100.0, "entity": "mira",
                       "source": "phone:owner", "asked": "ふふ"}])
        self._raw([{"at": 99.0, "entity": "mira", "role": "user",
                    "speaker": "owner", "source": "phone:owner",
                    "scene": "together", "text": "ふふ"}])
        result = apply(self.data, dry_run=True)
        self.assertEqual(result["rewrite_count"], 0)
        self.assertEqual(result["review_count"], 1)
        self.assertEqual(result["stats"].get("ambiguous_together_phone"), 1)

    # ── ③b owner 點名 ⇒ 才改 ──
    def test_owner_naming_marks_it_as_dm(self):
        self._outbox([{"turn_id": "t3", "at": 100.0, "entity": "mira",
                       "source": "phone:owner", "asked": "ふふ"}])
        self._raw([{"at": 99.0, "entity": "mira", "role": "user",
                    "speaker": "owner", "source": "phone:owner",
                    "scene": "together", "text": "ふふ"}])
        result = apply(self.data, dry_run=True, mark_dm=["t3"])
        self.assertEqual(result["rewrite_count"], 1)
        # ⚠️ 標籤主體是 **principal（說話的人）**，不是 entity（被叫的角色）
        #    ⇒ 與修復後 `DiscordSource._source_for()` 的 `discord:dm:{principal}` 一致。
        self.assertEqual(result["rewrites"][0]["new"], "discord:dm:owner")

    def test_owner_naming_app_keeps_it(self):
        self._outbox([{"turn_id": "t3", "at": 100.0, "entity": "mira",
                       "source": "phone:owner", "asked": "ふふ"}])
        self._raw([{"at": 99.0, "entity": "mira", "role": "user",
                    "speaker": "owner", "source": "phone:owner",
                    "scene": "together", "text": "ふふ"}])
        result = apply(self.data, dry_run=True, mark_app=["t3"])
        self.assertEqual(result["rewrite_count"], 0)
        self.assertEqual(result["review_count"], 0)

    def test_conflicting_marks_raise(self):
        self._outbox([{"turn_id": "t3", "at": 100.0, "entity": "mira",
                       "source": "phone:owner", "asked": "ふふ"}])
        self._raw([{"at": 99.0, "entity": "mira", "role": "user",
                    "speaker": "owner", "source": "phone:owner",
                    "scene": "together", "text": "ふふ"}])
        with self.assertRaises(ValueError):
            apply(self.data, dry_run=True, mark_dm=["t3"], mark_app=["t3"])

    # ── ③b 跨角色 ⇒ 判為私訊（不可能是 App 打字）──
    def test_cross_entity_segment_is_guessed_as_dm(self):
        self._outbox([
            {"turn_id": "c1", "at": 100.0, "entity": "mira", "principal": "owner",
             "source": "phone:owner", "asked": "A"},
            {"turn_id": "c2", "at": 110.0, "entity": "aster", "principal": "owner",
             "source": "phone:owner", "asked": "B"},
        ])
        self._raw([
            {"at": 99.0, "entity": "mira", "role": "user", "speaker": "owner",
             "source": "phone:owner", "scene": "together", "text": "A"},
            {"at": 109.0, "entity": "aster", "role": "user", "speaker": "owner",
             "source": "phone:owner", "scene": "together", "text": "B"},
        ])
        result = apply(self.data, dry_run=True)
        self.assertEqual(result["rewrite_count"], 2)
        self.assertEqual(result["stats"].get("guessed_dm"), 2)
        for item in result["rewrites"]:
            self.assertTrue(str(item["new"]).startswith("discord:dm:"))

    # ── ③c 同一句 × 多角色 ⇒ 群聊 fan-out，不是私訊 ──
    def test_fanout_same_ask_is_group_not_dm(self):
        self._outbox([
            {"turn_id": "f1", "at": 100.0, "entity": "mira", "principal": "owner",
             "source": "phone:owner", "asked": "在吗"},
            {"turn_id": "f2", "at": 100.2, "entity": "aster", "principal": "owner",
             "source": "phone:owner", "asked": "在吗"},
        ])
        self._raw([
            {"at": 99.0, "entity": "mira", "role": "user", "speaker": "owner",
             "source": "phone:owner", "scene": "together", "text": "在吗"},
            {"at": 99.2, "entity": "aster", "role": "user", "speaker": "owner",
             "source": "phone:owner", "scene": "together", "text": "在吗"},
        ])
        result = apply(self.data, dry_run=True)
        # 群聊 fan-out 不是私訊 ⇒ 不該被判成 discord:dm
        self.assertEqual(result["stats"].get("guessed_dm", 0), 0)

    # ── ③d 單角色連續 ⇒ 不猜（仍列 review）──
    def test_single_entity_segment_is_not_guessed(self):
        self._outbox([
            {"turn_id": "s1", "at": 100.0, "entity": "mira", "principal": "owner",
             "source": "phone:owner", "asked": "A"},
            {"turn_id": "s2", "at": 110.0, "entity": "mira", "principal": "owner",
             "source": "phone:owner", "asked": "B"},
        ])
        self._raw([
            {"at": 99.0, "entity": "mira", "role": "user", "speaker": "owner",
             "source": "phone:owner", "scene": "together", "text": "A"},
            {"at": 109.0, "entity": "mira", "role": "user", "speaker": "owner",
             "source": "phone:owner", "scene": "together", "text": "B"},
        ])
        result = apply(self.data, dry_run=True)
        self.assertEqual(result["stats"].get("guessed_dm", 0), 0)
        self.assertEqual(result["review_count"], 2)

    # ── ③e --no-guess-dm ⇒ 不做推測 ──
    def test_no_guess_dm_disables_inference(self):
        self._outbox([
            {"turn_id": "c1", "at": 100.0, "entity": "mira", "principal": "owner",
             "source": "phone:owner", "asked": "A"},
            {"turn_id": "c2", "at": 110.0, "entity": "aster", "principal": "owner",
             "source": "phone:owner", "asked": "B"},
        ])
        self._raw([
            {"at": 99.0, "entity": "mira", "role": "user", "speaker": "owner",
             "source": "phone:owner", "scene": "together", "text": "A"},
            {"at": 109.0, "entity": "aster", "role": "user", "speaker": "owner",
             "source": "phone:owner", "scene": "together", "text": "B"},
        ])
        result = apply(self.data, dry_run=True, guess_dm=False)
        self.assertEqual(result["stats"].get("guessed_dm", 0), 0)

    # ── ③f raw 對拍不上，但 owner 點名 ⇒ 仍然要改 ──
    def test_owner_named_dm_works_even_without_raw_match(self):
        # raw 完全沒有這一句（實測：raw 只記到前一輪，outbox 裡有下一輪）
        self._outbox([{"turn_id": "n1", "at": 100.0, "entity": "mira",
                       "principal": "owner", "source": "phone:owner",
                       "asked": "縫い目がほつれた方かなぁ"}])
        result = apply(self.data, dry_run=True, mark_dm=["n1"])
        self.assertEqual(result["rewrite_count"], 1)
        self.assertEqual(result["rewrites"][0]["new"], "discord:dm:owner")
        self.assertEqual(result["stats"].get("owner_named_dm_no_raw"), 1)

    def test_owner_named_app_works_even_without_raw_match(self):
        self._outbox([{"turn_id": "n1", "at": 100.0, "entity": "mira",
                       "principal": "owner", "source": "phone:owner",
                       "asked": "拉最新代码"}])
        result = apply(self.data, dry_run=True, mark_app=["n1"])
        self.assertEqual(result["rewrite_count"], 0)
        self.assertEqual(result["stats"].get("owner_named_app_no_raw"), 1)

    # ── ④ dry-run 不寫、apply 寫且備份 ──
    def test_dry_run_does_not_write(self):
        self._outbox([{"turn_id": "t1", "at": 100.0, "entity": "mira",
                       "source": "phone:owner", "asked": "在吗"}])
        self._raw([{"at": 99.0, "entity": "mira", "role": "user",
                    "speaker": "owner", "source": "discord:owner",
                    "scene": "discord-lobby", "text": "在吗"}])
        before = (self.data / "outbox" / "index.jsonl").read_text(encoding="utf-8")
        apply(self.data, dry_run=True)
        after = (self.data / "outbox" / "index.jsonl").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_apply_writes_and_backs_up(self):
        self._outbox([{"turn_id": "t1", "at": 100.0, "entity": "mira",
                       "source": "phone:owner", "asked": "在吗"}])
        self._raw([{"at": 99.0, "entity": "mira", "role": "user",
                    "speaker": "owner", "source": "discord:owner",
                    "scene": "discord-lobby", "text": "在吗"}])
        result = apply(self.data, dry_run=False)
        self.assertEqual(result["rewritten"], 1)
        backups = list((self.data / "outbox").glob("index.jsonl.bak-source-*"))
        self.assertEqual(len(backups), 1)
        rows = [json.loads(line) for line in
                (self.data / "outbox" / "index.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
        self.assertEqual(rows[0]["source"], "discord:owner")

    # ── raw 找不到 ⇒ 不動 ──
    def test_no_raw_match_is_skipped(self):
        self._outbox([{"turn_id": "t9", "at": 100.0, "entity": "mira",
                       "source": "phone:owner", "asked": "找不到"}])
        result = apply(self.data, dry_run=True)
        self.assertEqual(result["rewrite_count"], 0)
        self.assertEqual(result["stats"].get("no_raw_match"), 1)

    # ── 沒有 data 目錄時不爆 ──
    def test_missing_data_is_safe(self):
        empty = self.data / "empty"
        empty.mkdir()
        result = apply(empty, dry_run=True)
        self.assertEqual(result["rows"], 0)


if __name__ == "__main__":
    unittest.main()
