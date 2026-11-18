#!/usr/bin/env python3
"""TRPG 沉浸（owner 2026-10-23）：桌上說話的是**角色**，主人不在桌上就不准叫他。

判據：
  ① 主人不在這張桌 ⇒ prompt 明寫「不要提他／不要叫オーナー」；
  ② 主人的席位在桌上（名字是オーナー／主人）⇒ 允許以桌上角色身份跟他說，但仍不准叫「オーナー」；
  ③ 匯報那一支要明說是「桌外、對主人報告」，不能跟桌上發言混在一起；
  ④ 名字比對不分大小寫、也不吃空白。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from demo_common import *  # noqa: F401,F403

from agent.adapter.trpg import TrpgSeat


def seat_with(view: dict, *, owner_names: str = "") -> TrpgSeat:
    """不跑 __init__（那要接 hub），只把 _prompt 需要的狀態擺好。"""
    seat = TrpgSeat.__new__(TrpgSeat)
    seat.config = SimpleNamespace(seat="p5", owner_names=owner_names)
    seat.bans = {}
    seat.table = SimpleNamespace(view=lambda: view)
    return seat


def base_view(**extra) -> dict:
    view = {"table": {"roundNo": 2}, "you": {"seat": "p5", "name": "ミラ", "hp": 10,
                                             "max_hp": 12, "memory": 0, "max_memory": 80},
            "turn": {"seat": "p5"}, "board": {"you_are": {"cn": "白霧の森", "id": "n_forest"},
                                              "can_go": [], "party": []},
            "log": [], "seats": [{"seat": "p5", "name": "ミラ"},
                                 {"seat": "p2", "name": "ポーシア"}]}
    view.update(extra)
    return view


class TestTrpgImmersion(unittest.TestCase):
    def test_owner_absent_forbids_addressing_teacher(self):
        prompt = seat_with(base_view())._prompt(base_view(), {})
        self.assertIn("ご主人さまはこの卓にいない", prompt)
        self.assertIn("「オーナー／ご主人さま」とは呼ばない", prompt)
        self.assertIn("卓上のそのキャラクター", prompt)

    def test_owner_seated_allows_in_character_address(self):
        view = base_view(seats=[{"seat": "p5", "name": "ミラ"},
                                {"seat": "p6", "name": "オーナー"}])
        prompt = seat_with(view)._prompt(view, {})
        self.assertIn("ご主人さまもこの卓にいる", prompt)
        self.assertNotIn("ご主人さまはこの卓にいない", prompt)

    def test_owner_seat_detected_from_party(self):
        view = base_view(board={"you_are": {"cn": "白霧の森", "id": "n_forest"},
                                "can_go": [], "party": [{"name": "オーナー", "seat": "p7"}]})
        self.assertEqual(seat_with(view)._owner_seat(view), "p7")

    def test_owner_seat_is_case_insensitive(self):
        view = base_view(seats=[{"seat": "p6", "name": " Owner "}])
        self.assertEqual(seat_with(view)._owner_seat(view), "p6")

    def test_report_prompt_marks_out_of_table(self):
        prompt = seat_with(base_view())._report_prompt(base_view())
        self.assertIn("卓の外", prompt)
        self.assertIn("ご主人さまに今の状況", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
