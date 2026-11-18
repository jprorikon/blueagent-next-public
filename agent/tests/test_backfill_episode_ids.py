#!/usr/bin/env python3
"""舊封片 id 回填（tools/backfill_episode_ids.py）的判據。

要顧的三件事：
  ① 映射唯一：同角色、不同線各自從 1 開始的舊 id 要分得開；同線同 id 兩集要再分得開；
  ② 只改該改的字：raw 原文除了 `"episode"` 那一段以外**逐字不動**（原文是逐字權威檔）；
  ③ 重跑安全：已經是帶線後綴的 id 不再加後綴。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))
sys.path.insert(0, str(_HERE.parents[2] / "agent" / "tools"))

import backfill_episode_ids as backfill  # noqa: E402


def _record(episode_id, thread, *, start=100.0, end=200.0, summary="s"):
    return {"id": episode_id, "thread": thread, "started_at": start, "ended_at": end,
            "summary": summary, "turns": [], "entity": "mira"}


def _raw_row(at, thread, episode, text="原文一句"):
    return {"at": at, "thread": thread, "role": "user", "text": text, "episode": episode}


class TestPlanIds(unittest.TestCase):
    def test_legacy_ids_from_different_threads_get_different_ids(self):
        records = [_record("ep_mira-1", "th_aaa-1"), _record("ep_mira-1", "th_bbb-1")]
        new_ids, _, stats = backfill.plan_ids(records)
        self.assertEqual(new_ids, ["ep_mira-1-th_aaa_1", "ep_mira-1-th_bbb_1"])
        self.assertEqual((stats["change"], stats["already"]), (2, 0))

    def test_same_thread_two_episodes_with_same_id_are_split_by_start(self):
        records = [_record("ep_mira-15", "th_x-1", start=100.0, end=200.0),
                   _record("ep_mira-15", "th_x-1", start=900.0, end=950.0)]
        new_ids, _, stats = backfill.plan_ids(records)
        self.assertEqual(len(set(new_ids)), 2, new_ids)
        self.assertTrue(all(row.startswith("ep_mira-15-th_x_1-") for row in new_ids), new_ids)
        self.assertEqual(stats["collision_groups"], 1)

    def test_rewrite_records_of_one_episode_share_the_same_new_id(self):
        """同一集被改寫（同 id／同線／同開始、ended_at 不同）⇒ 同一個新 id，不是兩集。"""
        records = [_record("ep_mira-3", "th_x-1", start=100.0, end=200.0),
                   _record("ep_mira-3", "th_x-1", start=100.0, end=260.0)]
        new_ids, candidates, _ = backfill.plan_ids(records)
        self.assertEqual(new_ids[0], new_ids[1])
        rows = candidates[("ep_mira-3", "th_x-1")]
        self.assertEqual(len(rows), 1, "改寫版要合成一個候選（留最晚的結束時刻）")
        self.assertEqual(rows[0][2], 260.0)

    def test_already_migrated_ids_are_left_alone(self):
        records = [_record("ep_mira-1-th_aaa_1", "th_aaa-1")]
        new_ids, _, stats = backfill.plan_ids(records)
        self.assertEqual(new_ids, ["ep_mira-1-th_aaa_1"])
        self.assertEqual((stats["change"], stats["already"]), (0, 1))

    def test_hashed_ids_are_also_treated_as_migrated(self):
        correct = f"ep_mira-15-th_x_1-{backfill.short_hash(100.0)}"
        records = [_record(correct, "th_x-1", start=100.0)]
        new_ids, _, stats = backfill.plan_ids(records)
        self.assertEqual(new_ids, [correct])
        self.assertEqual(stats["already"], 1)

    def test_wrong_hash_is_corrected(self):
        """哈希和開始時刻對不上（手改過／舊規則算的）⇒ 只有 `--hash-all` 才會改回算得出來的那一個。"""
        records = [_record("ep_mira-15-th_x_1-ab12", "th_x-1", start=100.0)]
        new_ids, _, stats = backfill.plan_ids(records, hash_all=True)
        self.assertEqual(new_ids, [f"ep_mira-15-th_x_1-{backfill.short_hash(100.0)}"])
        self.assertEqual(stats["change"], 1)

    def test_record_without_thread_is_not_touched(self):
        records = [_record("ep_mira-7", "")]
        new_ids, _, stats = backfill.plan_ids(records)
        self.assertEqual(new_ids, ["ep_mira-7"])
        self.assertEqual(stats["no_thread"], 1)


class TestResolveRaw(unittest.TestCase):
    def setUp(self):
        records = [_record("ep_mira-1", "th_aaa-1", start=100.0, end=200.0),
                   _record("ep_mira-1", "th_bbb-1", start=300.0, end=400.0)]
        _, self.candidates, _ = backfill.plan_ids(records)

    def test_resolves_by_thread(self):
        row = _raw_row(150.0, "th_bbb-1", "ep_mira-1")
        self.assertEqual(backfill.resolve_raw(row["episode"], row["thread"], row["at"],
                                              self.candidates),
                         ("ep_mira-1-th_bbb_1", ""))

    def test_unknown_reference_is_not_guessed(self):
        self.assertEqual(backfill.resolve_raw("ep_mira-99", "th_aaa-1", 150.0,
                                              self.candidates),
                         ("ep_mira-99", "unknown"))

    def test_already_migrated_reference_is_left_alone(self):
        self.assertEqual(backfill.resolve_raw("ep_mira-1-th_aaa_1", "th_aaa-1", 150.0,
                                              self.candidates),
                         ("ep_mira-1-th_aaa_1", "already"))

    def test_overlapping_ranges_are_ambiguous_not_guessed(self):
        candidates = {("ep_mira-1", "th_x-1"): [("ep_a", 100.0, 300.0), ("ep_b", 150.0, 400.0)]}
        self.assertEqual(backfill.resolve_raw("ep_mira-1", "th_x-1", 200.0, candidates),
                         ("ep_mira-1", "ambiguous"))


class TestTextRewrite(unittest.TestCase):
    def test_only_the_episode_span_changes(self):
        line = json.dumps(_raw_row(150.0, "th_aaa-1", "ep_mira-1"), ensure_ascii=False)
        out = backfill.rewrite_episode_field(line, "ep_mira-1", "ep_mira-1-th_aaa_1")
        self.assertNotEqual(out, line)
        self.assertEqual(out.replace("ep_mira-1-th_aaa_1", "ep_mira-1"), line)

    def test_prefix_ids_do_not_match_longer_ids(self):
        """`ep_mira-1` 不能吃到 `ep_mira-15`（結尾引號要一起比）。"""
        line = json.dumps(_raw_row(150.0, "th_aaa-1", "ep_mira-15"), ensure_ascii=False)
        self.assertEqual(backfill.rewrite_episode_field(line, "ep_mira-1", "X"), line)

    def test_id_span_only_in_episodes_file(self):
        line = json.dumps(_record("ep_mira-1", "th_aaa-1"), ensure_ascii=False)
        out = backfill.rewrite_id_field(line, "ep_mira-1", "ep_mira-1-th_aaa_1")
        self.assertEqual(json.loads(out)["id"], "ep_mira-1-th_aaa_1")
        self.assertEqual({k: v for k, v in json.loads(out).items() if k != "id"},
                         {k: v for k, v in json.loads(line).items() if k != "id"})


class TestHashAll(unittest.TestCase):
    """`--hash-all`：連沒撞車的組也補哈希，把舊檔格式統一成跟新封的片一樣。"""

    def test_base_id_strips_fragment_and_hash(self):
        self.assertEqual(backfill.base_id("ep_mira-3", "th_x_1"), "ep_mira-3")
        self.assertEqual(backfill.base_id("ep_mira-3-th_x_1", "th_x_1"), "ep_mira-3")
        self.assertEqual(backfill.base_id("ep_mira-3-th_x_1-ab12", "th_x_1"), "ep_mira-3")

    def test_default_keeps_non_colliding_ids(self):
        records = [_record("ep_mira-1-th_x_1", "th_x-1", start=100.0)]
        new_ids, _, stats = backfill.plan_ids(records)
        self.assertEqual(new_ids, ["ep_mira-1-th_x_1"])
        self.assertEqual((stats["change"], stats["already"]), (0, 1))

    def test_hash_all_adds_hash_even_without_collision(self):
        records = [_record("ep_mira-1-th_x_1", "th_x-1", start=100.0),
                   _record("ep_mira-2", "th_x-1", start=300.0)]
        new_ids, _, stats = backfill.plan_ids(records, hash_all=True)
        self.assertEqual(new_ids[0], f"ep_mira-1-th_x_1-{backfill.short_hash(100.0)}")
        self.assertEqual(new_ids[1], f"ep_mira-2-th_x_1-{backfill.short_hash(300.0)}")
        self.assertEqual(stats["change"], 2)

    def test_hash_all_is_idempotent(self):
        records = [_record("ep_mira-1-th_x_1", "th_x-1", start=100.0)]
        first, _, _ = backfill.plan_ids(records, hash_all=True)
        records[0]["id"] = first[0]
        second, _, stats = backfill.plan_ids(records, hash_all=True)
        self.assertEqual(second, first)
        self.assertEqual((stats["change"], stats["already"]), (0, 1))

    def test_end_to_end_hash_all_updates_raw_and_reruns_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "raw" / "mira").mkdir(parents=True)
            records = [_record("ep_mira-1-th_x_1", "th_x-1", start=100.0, end=200.0)]
            (root / "episodes.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n",
                encoding="utf-8")
            raw = pathlib.Path(root / "raw" / "mira" / "2026-10.jsonl")
            raw.write_text(json.dumps(_raw_row(150.0, "th_x-1", "ep_mira-1-th_x_1"),
                                      ensure_ascii=False) + "\n", encoding="utf-8")
            self.assertEqual(backfill.main(["--data", str(root), "--apply", "--hash-all"]), 0)
            wanted = f"ep_mira-1-th_x_1-{backfill.short_hash(100.0)}"
            self.assertEqual(json.loads((root / "episodes.jsonl").read_text(encoding="utf-8"))["id"],
                             wanted)
            self.assertEqual(json.loads(raw.read_text(encoding="utf-8"))["episode"], wanted)
            before = ((root / "episodes.jsonl").read_text(encoding="utf-8"),
                      raw.read_text(encoding="utf-8"))
            self.assertEqual(backfill.main(["--data", str(root), "--apply", "--hash-all"]), 0)
            self.assertEqual(((root / "episodes.jsonl").read_text(encoding="utf-8"),
                              raw.read_text(encoding="utf-8")), before)


class TestEndToEnd(unittest.TestCase):
    def _make_data(self, tmp):
        root = pathlib.Path(tmp)
        (root / "raw" / "mira").mkdir(parents=True)
        records = [_record("ep_mira-1", "th_aaa-1", start=100.0, end=200.0),
                   _record("ep_mira-1", "th_bbb-1", start=300.0, end=400.0),
                   _record("ep_mira-1", "th_aaa-1", start=100.0, end=260.0, summary="改寫版")]
        (root / "episodes.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n", encoding="utf-8")
        rows = [_raw_row(150.0, "th_aaa-1", "ep_mira-1"),
                _raw_row(350.0, "th_bbb-1", "ep_mira-1"),
                _raw_row(160.0, "th_aaa-1", "")]          # 沒有 episode 欄位的行：一字不動
        rows[2].pop("episode")
        (root / "raw" / "mira" / "2026-10.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        return root

    def test_apply_makes_ids_unique_and_keeps_raw_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_data(tmp)
            before_raw = (root / "raw" / "mira" / "2026-10.jsonl").read_text(encoding="utf-8")
            self.assertEqual(backfill.main(["--data", str(root), "--apply"]), 0)
            episodes = [json.loads(line) for line in
                        (root / "episodes.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual({row["id"] for row in episodes},
                             {"ep_mira-1-th_aaa_1", "ep_mira-1-th_bbb_1"},
                             "兩條線分開；改寫版與本尊共用同一個 id（同一集）")
            self.assertEqual(episodes[0]["id"], episodes[2]["id"])
            after_raw = (root / "raw" / "mira" / "2026-10.jsonl").read_text(encoding="utf-8")
            after_lines = after_raw.splitlines()
            self.assertEqual(len(after_lines), 3)
            self.assertEqual([json.loads(line).get("episode") for line in after_lines],
                             ["ep_mira-1-th_aaa_1", "ep_mira-1-th_bbb_1", None])
            self.assertIn("原文一句", after_raw)
            backup = list((root / "raw" / "mira").glob("*.bak-id-*"))
            self.assertEqual(len(backup), 1)
            self.assertEqual(backup[0].read_text(encoding="utf-8"), before_raw)
            self.assertEqual(len(list(root.glob("episodes.jsonl.bak-id-*"))), 1)

    def test_second_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_data(tmp)
            backfill.main(["--data", str(root), "--apply"])
            episodes = (root / "episodes.jsonl").read_text(encoding="utf-8")
            raw = (root / "raw" / "mira" / "2026-10.jsonl").read_text(encoding="utf-8")
            self.assertEqual(backfill.main(["--data", str(root), "--apply"]), 0)
            self.assertEqual((root / "episodes.jsonl").read_text(encoding="utf-8"), episodes)
            self.assertEqual((root / "raw" / "mira" / "2026-10.jsonl").read_text(encoding="utf-8"), raw)


class TestUnresolvedBlocksWrites(unittest.TestCase):
    """review 2026-10-28 #1：raw 有歧義引用時，以前先寫了封片檔 ⇒ 重跑時舊引用失去對照表。"""

    def _make_ambiguous(self, tmp):
        root = pathlib.Path(tmp)
        (root / "raw" / "mira").mkdir(parents=True)
        # 同一條線兩筆舊 `ep_mira-1`，時間範圍重疊 ⇒ 落在重疊區的 raw 行是 ambiguous
        records = [_record("ep_mira-1", "th_x-1", start=100.0, end=300.0),
                   _record("ep_mira-1", "th_x-1", start=200.0, end=400.0)]
        (root / "episodes.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n",
            encoding="utf-8")
        raw = root / "raw" / "mira" / "2026-10.jsonl"
        raw.write_text(json.dumps(_raw_row(250.0, "th_x-1", "ep_mira-1"), ensure_ascii=False)
                       + "\n", encoding="utf-8")
        return root, raw

    def test_ambiguous_raw_reference_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, raw = self._make_ambiguous(tmp)
            before = ((root / "episodes.jsonl").read_text(encoding="utf-8"),
                      raw.read_text(encoding="utf-8"))
            self.assertEqual(backfill.main(["--data", str(root), "--apply"]), 3)
            self.assertEqual(((root / "episodes.jsonl").read_text(encoding="utf-8"),
                              raw.read_text(encoding="utf-8")), before, "一個檔都不能動")
            self.assertEqual(list(root.rglob("*.bak-id-*")), [], "沒寫就不該有備份")

    def test_rerun_after_refusal_still_sees_ambiguous_not_unknown(self):
        """★ 拒寫之後重跑：舊引用仍是 ambiguous（對照表還在），不會退化成 unknown。"""
        with tempfile.TemporaryDirectory() as tmp:
            root, _raw = self._make_ambiguous(tmp)
            backfill.main(["--data", str(root), "--apply"])
            _, stats = backfill.backfill_episodes(root / "episodes.jsonl", apply=False, stamp="x")
            totals = backfill._raw_totals(root, stats["candidates"], apply=False, stamp="x",
                                          report=False)
            self.assertEqual((totals["ambiguous"], totals["unknown"]), (1, 0))

    def test_allow_unresolved_writes_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, raw = self._make_ambiguous(tmp)
            self.assertEqual(backfill.main(["--data", str(root), "--apply", "--allow-unresolved"]), 1)
            ids = {json.loads(line)["id"] for line in
                   (root / "episodes.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()}
            self.assertTrue(all(item.startswith("ep_mira-1-th_x_1-") for item in ids), ids)
            self.assertEqual(json.loads(raw.read_text(encoding="utf-8"))["episode"], "ep_mira-1")


if __name__ == "__main__":
    unittest.main()
