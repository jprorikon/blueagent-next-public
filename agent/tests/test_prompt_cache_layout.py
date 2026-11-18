#!/usr/bin/env python3
"""提示詞排法（ADR-0020）：前綴快取能不能接上，是這一組的唯一判據。

判據（owner 2026-11-01 拍板「①改了試試」）：

  ① **前綴穩定**：第 N+1 回合的請求，前面那段必須和第 N 回合的請求逐條一樣
     （只允許尾巴多出「本輪段＋這句話」）——否則服務商的快取一個字都命不中；
  ② **每回合會變的東西在歷史之後**：記憶／時間感／承接／問句分類都算；
  ③ **窗口按檔位收縮**：條數超過 `max_history` 的 1.5 倍才一次砍回 `max_history`，
     不是每回合掉一條（每掉一條就等於每回合把前綴作廢一次）。

為什麼這三條要進測試：它們全是「看不出來」的性質——壞掉的時候回覆照樣正常，
只是帳單變貴、延遲變高。2026-11-01 在自家 key 上量到：舊排法第 5 回合命中 51.7%、
新排法 76.1%，未命中 token 少 28%。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.messages import WINDOW_STEP, build_messages
from agent.brain.ports import BrainRequest

META = {"recall": ["主人は『佐々木とピーちゃん』を六巻まで読んでいる"],
        "clock_block": "いま 12:00、7 往復目", "carry_summary": "前に小説の話をした"}


def request(turns: int, *, text: str, extra_meta=None) -> BrainRequest:
    """`turns` 個往復の話題履歴（user/assistant 成對）＋這一輪。"""
    transcript = []
    for index in range(turns):
        transcript.append({"role": "user", "text": f"u{index:03d} " + "あ" * 30})
        transcript.append({"role": "assistant", "text": f"a{index:03d} " + "い" * 30})
    meta = dict(META)
    meta.update(extra_meta or {})
    meta["transcript"] = transcript
    return BrainRequest(thread_id="th_x", entity="mira", principal="owner",
                        text=text, system="persona", meta=meta)


def contents(messages) -> list[str]:
    return [str(item.get("content") or "") for item in messages]


def stable_prefix(left: list[str], right: list[str]) -> int:
    """兩個請求從頭開始有幾條完全一樣。"""
    count = 0
    for one, other in zip(left, right):
        if one != other:
            break
        count += 1
    return count


def history_rows(messages: list[str]) -> int:
    return sum(1 for line in messages if line.startswith(("u0", "a0")))


class TestPrefixStability(unittest.TestCase):
    def test_next_turn_reuses_the_whole_prefix(self):
        """① 的判據：下一回合的請求，前面那段要和這一回合逐條相同。"""
        first = contents(build_messages(request(4, text="今日は寒いね")))
        second = contents(build_messages(request(5, text="続きを話そう")))
        stable = stable_prefix(first, second)
        self.assertGreaterEqual(
            stable, len(first) - 6,
            f"前綴只穩定了 {stable}/{len(first)} 條（第 {stable} 條就開始不一樣）"
            "——每回合變的東西跑到歷史前面去了？")
        self.assertLess(stable, len(first), "尾巴本來就該換（新的話＋新的本輪段）")

    def test_memory_block_sits_after_history_and_before_current(self):
        """記憶段在歷史之後、並且和「這句話」同在一條最後的用戶訊息裡（2026-11-02 改）。

        改的原因：Strata／llama-server 的快取只認「這次是上次的嚴格延伸」。本輪段若自成一條
        訊息卡在歷史之後，下一輪它就換了位置 ⇒ 不相符 ⇒ 整份重讀。併進最後一條用戶訊息後，
        它變成歷史的一部分，每輪只往後接。
        """
        messages = contents(build_messages(request(3, text="小説の話")))
        memory_at = next(index for index, text in enumerate(messages) if "[[memory]]" in text)
        history_at = next(index for index, text in enumerate(messages) if "u000" in text)
        self.assertGreater(memory_at, history_at, "記憶段仍在歷史之後")
        self.assertEqual(memory_at, len(messages) - 1,
                         "本輪段要併進最後一條用戶訊息，不再自成一條")

    def test_turn_blocks_can_be_sent_as_a_user_block(self):
        """不吃中途 system 的服務（llama-server／Qwen 模板會 500）⇒ 併成一條 user，位置不變。"""
        req = request(3, text="小説の話", extra_meta={"turn_blocks_role": "user"})
        messages = build_messages(req)
        contents = [item["content"] for item in messages]
        self.assertFalse(any(item["role"] == "system" and "[[memory]]" in item["content"]
                             for item in messages), "user 模式下不准再送中途 system")
        history_at = next(index for index, text in enumerate(contents) if "u000" in text)
        memory_at = next(index for index, text in enumerate(contents) if "[[memory]]" in text)
        self.assertGreater(memory_at, history_at, "位置一樣在歷史之後")
        self.assertEqual(messages[memory_at]["role"], "user")

    def test_changing_memory_does_not_move_the_prefix(self):
        """記憶換一句（每回合都會發生）⇒ 只有本輪段那條不同，前面整段不動。"""
        first = contents(build_messages(request(3, text="同じ質問")))
        changed = dict(META, recall=["主人は最近よく眠れていない"])
        second = contents(build_messages(request(3, text="同じ質問", extra_meta=changed)))
        self.assertGreaterEqual(stable_prefix(first, second), len(first) - 6)


class TestWindowRatchet(unittest.TestCase):
    def test_history_grows_to_one_and_a_half_then_snaps_back(self):
        """③ 的判據：漲到 1.5 倍之前一條不動，超過才一次砍回 N。"""
        limit = 6
        just_under = contents(build_messages(request(4, text="q"), max_history=limit))
        self.assertEqual(history_rows(just_under), 8, "8 條 ≤ 1.5×6，一條都不該砍")
        over = contents(build_messages(request(5, text="q"), max_history=limit))
        self.assertEqual(history_rows(over), limit, f"10 條 > 9 ⇒ 一次砍回 {limit} 條")

    def test_window_only_moves_on_the_step(self):
        """預算逼得比上限少時，條數落在 WINDOW_STEP 的檔位上（不是每回合掉一條）。"""
        sizes = []
        for turns in (5, 6, 7, 8):
            messages = contents(build_messages(request(turns, text="q"), max_chars=2200))
            sizes.append(history_rows(messages))
        for size in sizes:
            self.assertTrue(size <= WINDOW_STEP or size % WINDOW_STEP == 0,
                            f"窗口大小 {size} 不在檔位上：{sizes}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
