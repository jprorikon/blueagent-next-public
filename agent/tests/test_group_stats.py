#!/usr/bin/env python3
"""M5d：三人房六个指标（GROUP-SELF-CHAT §10）算得对、判得对。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent" / "tools"))

import group_stats as gs  # noqa: E402

T0 = 1_800_000_000.0


def msg(mid, who, text, at, kind=None):
    kind = kind or ("owner" if who == "owner" else "entity")
    return {"id": mid, "created_at": T0 + at, "author": {"id": who, "kind": kind},
            "parts": [{"kind": "text", "text": text}]}


class MetricCase(unittest.TestCase):
    def test_bubbles_of_one_turn_are_one_utterance(self):
        rows = [msg("a", "aster", "えっと", 0), msg("b", "aster", "それでね", 3), msg("c", "nori", "うん", 10),
                msg("d", "aster", "あとね", 60)]
        turns = gs.utterances(gs.normalize(rows))
        self.assertEqual([turn["who"] for turn in turns], ["aster", "nori", "aster"])

    def test_zero_reply_counts_owner_lines_nobody_answered(self):
        rows = [msg("o1", "owner", "おはよう", 0), msg("r1", "aster", "おはよう、オーナー", 30),
                msg("o2", "owner", "今日は寒いね", 1000),
                msg("o3", "owner", "誰かいる？", 3000), msg("k1", "nori", "いるよ", 3000 + 700)]
        zero, owners = gs.zero_reply(gs.utterances(gs.normalize(rows)))
        self.assertEqual(owners, 3)
        self.assertAlmostEqual(zero, 2 / 3, msg="o2 没人回、o3 的回复超过 10 分钟不算")

    def test_rotation_and_pingpong(self):
        order = ["aster", "nori", "mira", "aster", "nori", "aster"]
        rows = [msg(f"m{i}", who, "これは十字くらいの文", i * 40) for i, who in enumerate(order)]
        abc, aba, windows = gs.triples(gs.utterances(gs.normalize(rows)))
        self.assertEqual(windows, 4)
        self.assertAlmostEqual(abc, 3 / 4)       # r-k-m, k-m-r, m-r-k
        self.assertAlmostEqual(aba, 1 / 4)       # r-k-r

    def test_gap_median_ignores_long_breaks(self):
        rows = [msg("a", "aster", "x", 0), msg("b", "nori", "y", 30), msg("c", "aster", "z", 40),
                msg("d", "nori", "w", 40 + 5000)]
        gap, count = gs.gap_median(gs.utterances(gs.normalize(rows)))
        self.assertEqual((gap, count), (20.0, 2))

    def test_read_no_reply_uses_ledger_seen_decisions(self):
        rows = gs.normalize([msg("o1", "owner", "ねえ", 0), msg("r1", "aster", "なに？", 40)])
        ledger = {"decisions": {"cnv\0o1\0aster": [True, 30.0, "notify"],
                                "cnv\0o1\0nori": [True, 50.0, "notify"],
                                "cnv\0o1\0mira": [False, 0.0, "notify"]}}
        quiet, seen = gs.read_no_reply(rows, ledger, "cnv")
        self.assertEqual((quiet, seen), (0.5, 2), "nori 看到了没回；没看到的ミラ不算")
        self.assertEqual(gs.read_no_reply(rows, None), (None, 0))

    def test_short_lines_print_both_cuts_and_mode(self):
        texts = ["うん", "そうなんだ", "それ私も見たよ", "今日はちょっと寒い", "あとで一緒に行こう",
                 "わかった"]
        rows = gs.normalize([msg(f"m{i}", "aster", text, i) for i, text in enumerate(texts)])
        short = gs.short_lines(rows)
        self.assertEqual(short["count"], 6)
        self.assertAlmostEqual(short["le5"], 3 / 6)
        self.assertAlmostEqual(short["le10"], 1.0)
        self.assertIn(short["mode_bucket"], {"1-5", "6-10"})


class ReportCase(unittest.TestCase):
    def test_since_cuts_old_program_data_and_verdicts_render(self):
        rows = [msg("old", "aster", "前のバージョン", -100),
                msg("o1", "owner", "おはよう", 0), msg("r1", "aster", "おはようございます", 10),
                msg("k1", "nori", "おはよ〜みんな", 22), msg("m1", "mira", "おはようございます", 35),
                msg("o2", "owner", "寒いね", 400)]
        stats = gs.compute(rows, since=T0)
        self.assertEqual(stats["messages"], 5)
        self.assertTrue(stats["verdict"]["zero_reply"], "o2 没人回 ⇒ 1/2 > 20%")
        text = gs.report(stats)
        for needle in ("owner 一句 → 0 人回", "rotation_abc", "pingpong_aba", "相邻间隔中位",
                       "已读不回", "≤5 字", "≤10 字", "量到的", "先验"):
            self.assertIn(needle, text)

    def test_cli_reads_an_export_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "room.json"
            path.write_text(json.dumps({"messages": [msg("o1", "owner", "やあ", 0)]}), encoding="utf-8")
            self.assertEqual(gs.main(["--messages", str(path), "--ledger", str(path.with_name("none.json")),
                                      "--json"]), 0)

    def test_since_accepts_a_local_datetime(self):
        self.assertGreater(gs._since("2026-11-08 07:00"), 1_700_000_000)
        self.assertEqual(gs._since(""), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
