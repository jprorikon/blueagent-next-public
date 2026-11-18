#!/usr/bin/env python3
"""群聊「這句話在對誰說」（`runtime/addressee.py`）。

owner 2026-10-27 回報：他在 Discord 群裡跟名單外的人、甚至跟另一個 bot（Hermes）一來一回，
她都會插話。舊規則的兩個漏洞都寫在模組 docstring；這裡逐條鎖住新行為。
"""

from __future__ import annotations

import unittest

from agent.brain.runtime.addressee import ENTITY, UNKNOWN, AddresseeTracker

ME = ("mira", "ミラ", "ミラ")
OWNER = "owner-1"
OTHER = "guest-9"
BOT = "hermes-1"
CH = "ch-1"


class TestAddressee(unittest.TestCase):
    def setUp(self):
        self.tracker = AddresseeTracker(window_s=180.0)
        self.roster = {OWNER: "船長", OTHER: "ノリ", BOT: "Hermes"}

    def resolve(self, text="", author=OWNER, now=1000.0, **kwargs):
        return self.tracker.resolve(channel_id=CH, author_id=author, text=text,
                                    my_names=ME, roster=dict(self.roster), now=now, **kwargs)

    def test_direct_mention_is_hers(self):
        self.assertEqual(self.resolve("これ見て", mentioned_me=True)[0], ENTITY)

    def test_reply_to_her_is_hers(self):
        self.assertEqual(self.resolve("そうだね", reply_to_me=True)[0], ENTITY)

    def test_name_in_sentence_start_is_hers(self):
        self.assertEqual(self.resolve("ミラ、これ見て")[0], ENTITY)

    def test_plain_talk_without_signals_is_not_hers(self):
        """★ 這條是這次的 bug：主人隨便講一句，她以前會接。"""
        self.assertEqual(self.resolve("今日はいい天気だね")[0], UNKNOWN)

    def test_mentioning_someone_else_is_not_hers(self):
        addressee, reason = self.resolve("ノリ、これ分かる？", mentioned_other_ids=[OTHER])
        self.assertEqual((addressee, reason), (f"other:{OTHER}", "mention_other"))

    def test_replying_to_someone_else_is_not_hers(self):
        addressee, reason = self.resolve("そうそう", reply_other_id=OTHER)
        self.assertEqual((addressee, reason), (f"other:{OTHER}", "reply_other"))

    def test_naming_someone_else_is_not_hers(self):
        addressee, reason = self.resolve("ノリ、これ分かる？")
        self.assertEqual((addressee, reason), (f"other:{OTHER}", "named_other"))

    def test_engaged_only_continues_with_the_same_speaker(self):
        self.resolve("ミラ、これ見て", now=1000.0)             # 她被他叫到 ⇒ 進入 engaged
        self.assertEqual(self.resolve("で、どう思う？", now=1010.0)[1], "engaged")
        # 換人講話 ⇒ 解除；再換回主人也不算延續
        self.tracker.note_other_speaker(CH, OTHER, now=1020.0)
        self.assertEqual(self.resolve("で、どう思う？", now=1030.0)[0], UNKNOWN)

    def test_another_bot_speaking_clears_the_engaged_state(self):
        """★ 主人 ↔ Hermes 的一來一回：Hermes 一回話就解除，主人下一句不再算對她說。"""
        self.resolve("ミラ、いる？", now=1000.0)
        self.tracker.note_other_speaker(CH, BOT, now=1005.0)      # Hermes 回覆
        addressee, reason = self.resolve("じゃあ明日の件だけど", now=1010.0)
        self.assertEqual((addressee, reason), (UNKNOWN, "no_signal"))

    def test_unlisted_human_speaking_clears_the_engaged_state(self):
        """★ 名單外的**真人**插一句也要解除（以前只有 bot 會清）：主人回他時她不該接。"""
        self.resolve("ミラ、いる？", now=1000.0)
        self.resolve("こんにちは〜", author=OTHER, now=1005.0)      # 名單外的人開口
        addressee, reason = self.resolve("そうだね、いいと思う", now=1010.0)
        self.assertEqual((addressee, reason), (UNKNOWN, "no_signal"))

    def test_calling_another_character_first_wins(self):
        """★「ノリ、ミラはどう思う？」是在問ノリ——句首叫的是誰就算誰。"""
        addressee, reason = self.resolve("ノリ、ミラはどう思う？", other_names=("ノリ",))
        self.assertEqual((addressee, reason), ("other:ノリ", "named_other_character"))

    def test_calling_a_roster_member_first_wins(self):
        addressee, _reason = self.resolve("船長、ミラはどう思う？", author=OTHER)
        self.assertEqual(addressee, f"other:{OWNER}")

    def test_topic_mention_mid_sentence_is_not_a_call(self):
        """句中「ミラは／ミラさんが」是在談她，不是叫她。"""
        self.assertEqual(self.resolve("昨日ミラは来なかった")[0], UNKNOWN)
        self.assertEqual(self.resolve("昨日ミラさんが言ってた")[0], UNKNOWN)

    def test_mid_sentence_vocative_still_counts(self):
        self.assertEqual(self.resolve("これどう思う、ミラ？")[0], ENTITY)
        self.assertEqual(self.resolve("ねえ、ミラさん！")[0], ENTITY)

    def test_her_name_first_beats_a_later_other_name(self):
        self.assertEqual(self.resolve("ミラ、ノリはどう思うかな？",
                                      other_names=("ノリ",))[0], ENTITY)

    def test_engaged_expires(self):
        self.resolve("ミラ、これ見て", now=1000.0)
        self.assertEqual(self.resolve("で、どう思う？", now=1000.0 + 181.0)[0], UNKNOWN)

    def test_talking_to_someone_else_clears_engaged(self):
        self.resolve("ミラ、これ見て", now=1000.0)
        self.resolve("ノリ、これ分かる？", mentioned_other_ids=[OTHER], now=1005.0)
        self.assertEqual(self.resolve("で、どう思う？", now=1010.0)[0], UNKNOWN)

    def test_window_zero_never_stays_engaged(self):
        tracker = AddresseeTracker(window_s=0.0)
        tracker.resolve(channel_id=CH, author_id=OWNER, text="ミラ、これ見て",
                        my_names=ME, roster=dict(self.roster), now=1000.0)
        addressee, _reason = tracker.resolve(channel_id=CH, author_id=OWNER, text="で？",
                                             my_names=ME, roster=dict(self.roster), now=1000.0)
        self.assertEqual(addressee, UNKNOWN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
