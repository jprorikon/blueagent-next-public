#!/usr/bin/env python3
"""群聊点名判定（REVIEW 2026-11-08 A4）：只认 `@名字` 或句首叫名；ASCII 名字要词边界。"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.adapter.app_mentions import mentioned  # noqa: E402

ALIASES = {"aster": ("aster", "アスター", "アスター"), "nori": ("nori", "ノリ", "ノリ"),
           "mira": ("mira", "ミラ", "ミラ")}


class MentionCase(unittest.TestCase):
    def test_at_mention_anywhere(self):
        self.assertEqual(mentioned("その写真、@ミラ あとで見せて", ALIASES), {"mira"})
        self.assertEqual(mentioned("@Aster look at this", ALIASES), {"aster"})

    def test_name_at_sentence_start(self):
        self.assertEqual(mentioned("ミラ、それどう思う？", ALIASES), {"mira"})
        self.assertEqual(mentioned("「ノリ、来て」", ALIASES), {"nori"})
        self.assertEqual(mentioned("Aster, are you there?", ALIASES), {"aster"})

    def test_third_person_mid_sentence_is_not_a_call(self):
        self.assertEqual(mentioned("昨日ミラが言ってた件、みんなどう思う？", ALIASES), set())

    def test_ascii_name_inside_a_word_is_not_a_call(self):
        self.assertEqual(mentioned("this period is long", ALIASES), set())
        self.assertEqual(mentioned("riot was loud", ALIASES), set())
        self.assertEqual(mentioned("@riot", ALIASES), set())

    def test_several_called(self):
        self.assertEqual(mentioned("@アスター @ノリ どっちがいい？", ALIASES), {"aster", "nori"})

    def test_empty(self):
        self.assertEqual(mentioned("", ALIASES), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
