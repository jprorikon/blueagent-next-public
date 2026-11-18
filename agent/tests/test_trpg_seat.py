#!/usr/bin/env python3
"""她上 TRPG 桌：kick → 看牌面 → 說話＋宣告合法動作。

判據：
  ① 不是她的回合／這一輪已出手 ⇒ 不出手（不亂動別人的手）；
  ② 從她那句自然語言挑出**合法**動作（攻擊／移動／搜尋…），挑不到就挑第一個非交棒、非攻擊的動作
     （2026-11-09：沒說清楚不替她出手打；否定與「待つ」見 `test_trpg_review_20261109.py`）；
  ③ 移動會挑她提到的格子，沒提到就去第一個可去的；
  ④ 引擎拒絕 ⇒ 讓她看錯誤改一手（有上限，最多再一次）；
  ⑤ 她說的話一定先 `table_say`（動作被拒不代表台詞沒說）；
  ⑥ kick 疊車有鎖（重送的 kick 判 busy，不在同一手做兩次）。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.trpg import (TrpgConfig, TrpgSeat, choose_action,
                                              render_view)

SEAT = "p1"


def view(**overrides) -> dict:
    base = {
        "table": {"roundNo": 3},
        "you": {"seat": SEAT, "name": "ミラ", "hp": 8, "max_hp": 10,
                "memory": 4, "max_memory": 6, "star_coins": 2},
        "turn": {"seat": SEAT},
        "board": {"you_are": {"id": "n_forest", "cn": "森", "stellaroids_left": 2},
                  "can_go": [{"to": "flavio", "cn": "フラヴィオ"},
                             {"to": "amor", "cn": "アモール"}],
                  "party": [{"name": "オーナー", "where_cn": "森", "node": "n_forest"}]},
        "allowed_actions": ["move", "fight", "search", "say", "end_turn"],
        "log": [{"type": "say", "seat": "p2", "text": "先行くよ"},
                {"type": "move", "seat": "p2", "text": "p2 が flavio へ"}],
        "your_goal": {"id": "g1", "title": "塔の記憶を取り戻す"},
    }
    base.update(overrides)
    return base


class FakeTable:
    def __init__(self, view_data: dict, *, reject_first: bool = False) -> None:
        self.view_data = view_data
        self.said: list[str] = []
        self.acted: list[tuple] = []
        self.reject_first = reject_first
        self.views = 0

    def view(self, seat=None):
        self.views += 1
        return dict(self.view_data)

    def say(self, text):
        self.said.append(text)
        return {"ok": True}

    def act(self, action, *, target=None, note=None, skill=None, item=None):
        self.acted.append((action, target))
        if self.reject_first and len(self.acted) == 1:
            return {"ok": False, "error": "入口現在關著，要在 mon_n 用 search 重開"}
        return {"ok": True}


def seat(table, replies: list[str], **kwargs) -> TrpgSeat:
    queue = list(replies)

    def speak(prompt: str, purpose: str = "trpg") -> str:
        return queue.pop(0) if queue else ""

    config = TrpgConfig(enabled=True, table_id="t1", session="s1", seat=SEAT,
                        entity="mira", kick_token="tok")
    return TrpgSeat(config, table, speak, **kwargs)


class TestRender(unittest.TestCase):
    def test_view_has_the_things_she_needs(self):
        text = render_view(view(), SEAT)
        for needle in ("ミラ", "森", "行ける場所", "フラヴィオ", "使える行動", "オーナー"):
            self.assertIn(needle, text, f"牌面缺少 {needle}：\n{text}")

    def test_says_are_labelled(self):
        text = render_view(view(), SEAT)
        self.assertIn("さっきの発言", text)
        self.assertIn("先行くよ", text)

    def test_view_is_japanese(self):
        """★ review 2026-10-28：牌面以前整段繁中（第 N 輪／輪到／剛剛說話／可用動作）。"""
        from prompt_lang import chinese_hits
        full = view(turn={"seat": SEAT, "seconds_left": 20},
                    board={**view()["board"], "entrance_open_rounds_left": 2})
        self.assertEqual(chinese_hits(render_view(full, SEAT)), [])

    def test_japanese_names_win_over_chinese(self):
        text = render_view(view(board={"you_are": {"id": "n1", "cn": "森林", "jp": "森"}}), SEAT)
        self.assertIn("現在地：森（n1）", text)


class TestChooseAction(unittest.TestCase):
    def test_attack_word_maps_to_fight(self):
        self.assertEqual(choose_action(view(), "今の星骸を斬る！")["action"], "fight")

    def test_move_picks_the_node_she_mentioned(self):
        picked = choose_action(view(), "アモールへ戻ろう")
        self.assertEqual(picked["action"], "move")
        self.assertEqual(picked["target"], "amor")

    def test_move_without_target_goes_to_first_neighbour(self):
        picked = choose_action(view(), "先へ進む")
        self.assertEqual(picked["action"], "move")
        self.assertEqual(picked["target"], "flavio")

    def test_search_and_pray(self):
        self.assertEqual(choose_action(view(), "入口を探す")["action"], "search")
        allowed_pray = view(allowed_actions=["move", "pray", "end_turn"])
        self.assertEqual(choose_action(allowed_pray, "ここで祈る")["action"], "pray")

    def test_unknown_intent_still_acts_legally(self):
        picked = choose_action(view(), "うーん")
        self.assertIn(picked["action"], {"move", "fight", "search"})
        self.assertNotEqual(picked["action"], "end_turn")

    def test_nothing_allowed_means_end_turn(self):
        self.assertEqual(choose_action(view(allowed_actions=["end_turn"]), "どうしよう")["action"],
                         "end_turn")

    def test_illegal_action_is_not_invented(self):
        picked = choose_action(view(allowed_actions=["move", "end_turn"]), "祈る！")
        self.assertNotEqual(picked["action"], "pray", "引擎沒給的動作不能宣告")


class TestStep(unittest.TestCase):
    def test_not_my_turn_does_nothing(self):
        table = FakeTable(view(turn={"seat": "p2"}))
        who = seat(table, ["私の番？"])
        result = who.step({})
        self.assertEqual(result.get("skipped"), "not_my_turn")
        self.assertEqual(table.acted, [])
        self.assertEqual(table.said, [])

    def test_speaks_then_acts(self):
        table = FakeTable(view())
        who = seat(table, ["星骸を斬るよ！"])
        result = who.step({})
        self.assertTrue(result["ok"], result)
        self.assertEqual(table.said, ["星骸を斬るよ！"], "台詞要先說")
        self.assertEqual(table.acted[0][0], "fight")
        self.assertEqual(who.said, 1)

    def test_passes_turn_when_it_stays_hers(self):
        """`end_turn_on_action=false` ＋ move 是 free action ⇒ 她必須自己交棒，不然整桌卡住。"""
        table = FakeTable(view())           # 牌面一直顯示輪到 p1
        who = seat(table, ["アモールへ行く"])
        who.step({})
        self.assertEqual(table.acted[-1][0], "end_turn", f"出手後要交棒：{table.acted}")

    def test_already_acted_means_pass(self):
        table = FakeTable(view(you_acted=True))
        who = seat(table, [])
        result = who.step({})
        self.assertEqual(result.get("skipped"), "you_acted")
        self.assertEqual(table.acted, [("end_turn", None)])

    def test_rejection_triggers_one_retry(self):
        table = FakeTable(view(), reject_first=True)
        who = seat(table, ["塔に入る！", "じゃあ入口を探す"])
        result = who.step({})
        self.assertTrue(result["ok"], result)
        self.assertEqual(table.acted[1][0], "search", f"被拒後第二手應改搜尋：{table.acted}")
        self.assertEqual(table.acted[-1][0], "end_turn", "最後要交棒")
        self.assertEqual(who.rejected, 1)
        self.assertEqual(len(table.said), 2, "改手時也該說話")

    def test_empty_reply_passes_turn(self):
        table = FakeTable(view())
        who = seat(table, [""])
        result = who.step({})
        self.assertEqual(result.get("skipped"), "no_text")
        self.assertEqual(table.acted, [("end_turn", None)])

    def test_gone_table_clears_the_binding(self):
        """桌被刪／hub 重啟 ⇒ 清掉 table／session（不然狀態看起來還在某一桌玩）。"""
        class Broken:
            def view(self, seat=None):
                raise RuntimeError("HTTP Error 404: Not Found")

            def say(self, text):
                return {"ok": True}

            def act(self, action, **kwargs):
                return {"ok": True}

        config = TrpgConfig(enabled=True, table_id="dead_table", session="dead_session",
                            seat=SEAT)
        who = TrpgSeat(config, Broken(), lambda prompt, purpose="trpg": "…")
        result = who.step({})
        self.assertEqual(result.get("skipped"), "no_table")
        self.assertEqual(config.table_id, "")
        self.assertEqual(config.session, "")

    def test_ended_table_is_silent(self):
        table = FakeTable(view(ended=True))
        who = seat(table, [])
        self.assertEqual(who.step({}).get("skipped"), "ended")
        self.assertEqual(table.acted, [])

    def test_busy_lock_prevents_double_action(self):
        table = FakeTable(view())
        who = seat(table, ["斬る", "斬る"])
        who._lock.acquire()                     # 模擬一手還在跑
        try:
            result = who.handle_kick({})
        finally:
            who._lock.release()
        self.assertEqual(result.get("skipped"), "busy")
        self.assertEqual(table.acted, [], "重送的 kick 不能讓她出兩手")


if __name__ == "__main__":
    unittest.main(verbosity=2)
