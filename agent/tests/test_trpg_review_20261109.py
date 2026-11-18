#!/usr/bin/env python3
"""TRPG 檢查（owner 2026-11-09「检查一下 trpg 相关的代码」）找到的問題，一條一個判據。

  ① 桌上的事件真的進心情（以前 `appraise(target=game)` 每筆 INVALID_INPUT，從沒進過）；
  ② 桌喊她出手不算「主人說話／回了／在 App 上」（以前每一手都把主動消息記成已回、
     心情當成主人陪聊、主人缺席計時歸零）；預想也不把桌的 prompt 當成主人的話來想；
  ③ 挑動作：「待とう／休みたい」＝這手不動；「攻撃はしない」不是要攻擊；
     沒說清楚時不替她出手打（以前四句全被挑成 fight）；
  ④ 被拒的動作按**送出去的那個**記（以前讀引擎回覆的 `action`，拒絕回覆沒有 ⇒ 永遠記不到）；
  ⑤ 新的一局把這一局的帳歸零（事件 seq、死亡磨損、禁用動作）；磨損封頂 1.0；別人席位的事不算她的；
  ⑥ 一手在跑時，另一張桌的 kick 不能改掉這一手的桌／席位；
  ⑦ 拒玩／匯報門檻在現在的情緒尺上搆得到；
  ⑧ 桌端的 kick prompt 不蓋掉「這是遊戲／主人不在桌上」的規矩；沒 HP 欄位不亂喊 HP 低；
     MCP 握手失敗會重試。
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.trpg import TrpgConfig, TrpgMcp, TrpgSeat, choose_action
from agent.brain.runtime.inner.affect import AffectService, _label
from agent.server.integrations.trpg import make_game_affect

SEAT = "p1"


def view(**overrides) -> dict:
    base = {"table": {"roundNo": 2}, "turn": {"seat": "p9"},
            "you": {"seat": SEAT, "hp": 8, "max_hp": 10},
            "board": {"can_go": [{"to": "n2", "jp": "アモール"}], "you_are": {"stellaroid_uid": "s1"}},
            "allowed_actions": ["fight", "move", "search", "end_turn"], "log": []}
    base.update(overrides)
    return base


class Table:
    def __init__(self, data: dict, *, reject: int = 0) -> None:
        self.data = data
        self.config = TrpgConfig(enabled=True, table_id="t1", session="s1", seat=SEAT,
                                 kick_token="k")
        self.reject = reject
        self.acted: list[tuple] = []
        self.said: list[str] = []

    def view(self, seat=None):
        return dict(self.data)

    def say(self, text):
        self.said.append(text)
        return {"ok": True}

    def act(self, action, *, target=None, note=None, skill=None, item=None):
        self.acted.append((action, target))
        if action != "end_turn" and self.reject > 0:
            self.reject -= 1
            return {"ok": False, "error": "その行動は今はできない"}   # 引擎的拒絕回覆沒有 `action`
        return {"ok": True}


def make_seat(table, replies=("攻撃する！",), **kwargs) -> TrpgSeat:
    queue = list(replies)
    config = TrpgConfig(enabled=True, table_id="t1", session="s1", seat=SEAT, kick_token="k")
    return TrpgSeat(config, table, lambda prompt, purpose="trpg": queue.pop(0) if queue else "",
                    **kwargs)


class TestGameEventsReachMood(unittest.TestCase):
    def test_death_and_level_up_move_valence(self) -> None:
        affect = AffectService(tempfile.mkdtemp())
        state = type("S", (), {"app": type("A", (), {"affect": affect})()})()
        config = TrpgConfig(table_id="t1", session="s1", seat=SEAT, entity="mira")
        game = make_game_affect(state, config)
        before = affect.mood("mira")["valence"]
        game("death", -0.22, -0.10, {"seq": 1})
        after_death = affect.mood("mira")["valence"]
        self.assertLess(after_death, before, "死了要真的進心情（以前每筆 INVALID_INPUT）")
        game("level_up", 0.12, 0.04, {"seq": 2})
        self.assertGreater(affect.mood("mira")["valence"], after_death)


class TestTableTurnIsNotOwnerTalk(unittest.TestCase):
    def setUp(self) -> None:
        from agent.brain.compose.state import AppState
        self.demo = build(data_dir=tempfile.mkdtemp())
        self.state = AppState(self.demo)

    def test_trpg_hand_does_not_mark_owner_heard_or_reset_absence(self) -> None:
        proactive = self.demo.proactive
        valence = lambda: float(self.demo.affect.mood("mira")["valence"])  # noqa: E731
        before = valence()
        result = self.state.send("（あなたは今 TRPG を遊んでいる。盤面を見て、この手を決めて。）",
                                 entity="mira", principal="owner", force_reply=True,
                                 mode="chat", source_override="trpg:table-1")
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(proactive.heard_at("mira"), 0.0, "桌喊她出手 ≠ 主人回了")
        after_table = valence()
        self.assertLess(after_table - before, 0.01, "不是主人陪她聊（以前每手 +0.05）")
        # 對照：主人真的在 App 說一句，照樣算（回了＋心情被牽動）
        self.state.send("ただいま", entity="mira", principal="owner", force_reply=True,
                        mode="chat", source_override="app:owner")
        self.assertGreater(proactive.heard_at("mira"), 0.0)
        self.assertGreater(valence() - after_table, 0.02)

    def test_prethink_skips_table_rows(self) -> None:
        from agent.brain.runtime.prethink import PreThinker
        now = time.time()
        rows = [{"at": now - 900, "thread": "th", "turn": f"t{i}", "role": "user", "speaker": "owner",
                 "source": "trpg:table-1", "text": "（あなたは今 TRPG を遊んでいる）",
                 "room": ["mira", "owner"]} for i in range(3)]

        class Raw:
            def recent(self, entity, limit=12):
                return rows[-limit:]

        prompts = []
        thinker = PreThinker(raw_log=Raw(), call=lambda p: prompts.append(p) or "x",
                             root=tempfile.mkdtemp(), entities=["mira"], idle_s=60,
                             interval_s=30)
        thinker.think_once("mira")
        self.assertFalse(any("TRPG を遊んでいる" in prompt for prompt in prompts),
                         "桌的 prompt 不是主人的話，不拿來當對話想")


class TestChooseAction(unittest.TestCase):
    CASES = {
        "今は様子を見て、ここで待とう。": "end_turn",
        "うーん、少し休みたい……": "end_turn",
        "不攻击，先等等": "end_turn",
        "攻撃はしない、ここから逃げる。": "move",
        "打ち合わせ通り、北へ行こう": "move",
        "よし、攻撃する！": "fight",
        "あの星骸を斬る": "fight",
        "ここを調べてみる": "search",
    }

    def test_cases(self) -> None:
        for text, expected in self.CASES.items():
            with self.subTest(text=text):
                self.assertEqual(choose_action(view(), text)["action"], expected)

    def test_no_clear_intent_never_attacks(self) -> None:
        self.assertNotEqual(choose_action(view(), "……")["action"], "fight")
        self.assertEqual(choose_action({"allowed_actions": ["fight", "end_turn"]}, "……")["action"],
                         "end_turn", "只剩攻擊可選 ⇒ 交棒，不替她動手打")


class TestBansAndNewGame(unittest.TestCase):
    def test_rejected_action_is_banned_by_what_was_sent(self) -> None:
        table = Table(view(turn={"seat": SEAT}), reject=2)
        seat = make_seat(table, replies=("攻撃する！", "やっぱり攻撃！"))
        seat.handle_kick({})
        self.assertEqual(seat.bans.get("fight"), 2, "拒絕回覆沒有 action 也要記得是 fight 被拒")
        self.assertIn("fight", seat._prompt(view(turn={"seat": SEAT}), {}).split("⛔")[-1])

    def test_new_table_resets_this_games_books(self) -> None:
        table = Table(view())
        seat = make_seat(table)
        seat.bans = {"fight": 3}
        seat._event_counts = {"death": 5}
        seat._seen_seq = 40
        seat.handle_kick({"table_id": "t2", "session": "s9"})
        self.assertEqual((seat.bans, seat._event_counts), ({}, {}))
        self.assertEqual(table.config.table_id, "t2")

    def test_first_look_is_a_baseline_then_only_her_events(self) -> None:
        seen = []
        table = Table(view(log=[{"seq": 1, "type": "death", "seat": SEAT}]))
        seat = make_seat(table, affect=lambda kind, dv, da, row: seen.append((kind, row.get("seat"))))
        seat.handle_kick({})
        self.assertEqual(seen, [], "開局以來的舊事件不一口氣吞下")
        table.data = view(log=[{"seq": 2, "type": "level_up", "seat": "p2"},
                               {"seq": 3, "type": "damage", "seat": SEAT},
                               {"seq": 4, "type": "ended"}])
        seat.handle_kick({})
        self.assertEqual([kind for kind, _ in seen], ["damage", "ended"],
                         "別人升級不算她的；她自己的與全桌的才算")

    def test_wear_never_amplifies_past_full_value(self) -> None:
        deltas = []
        table = Table(view(log=[]))
        seat = make_seat(table, affect=lambda kind, dv, da, row: deltas.append(dv))
        seat.handle_kick({})
        table.data = view(log=[{"seq": n, "type": "death", "seat": SEAT} for n in range(1, 12)])
        seat.handle_kick({})
        self.assertGreaterEqual(min(deltas), -0.22 - 1e-9, "磨損封頂：最多算到這件事的原值")


class TestLockBeforeAdopt(unittest.TestCase):
    def test_kick_for_another_table_does_not_hijack_a_running_hand(self) -> None:
        gate = threading.Event()
        table = Table(view(turn={"seat": SEAT}))

        def slow(prompt, purpose="trpg"):
            gate.wait(2)
            return "攻撃する！"

        config = TrpgConfig(enabled=True, table_id="t1", session="s1", seat=SEAT, kick_token="k")
        seat = TrpgSeat(config, table, slow)
        worker = threading.Thread(target=seat.handle_kick, args=({},))
        worker.start()
        time.sleep(0.1)
        busy = seat.handle_kick({"table_id": "t2", "session": "s2", "seat": "p7"})
        self.assertEqual(busy.get("skipped"), "busy")
        self.assertEqual((config.table_id, config.seat), ("t1", SEAT), "跑到一半的那一手不被換桌")
        gate.set()
        worker.join(3)


class TestThresholdsAndPrompt(unittest.TestCase):
    def test_defaults_are_reachable_on_todays_scale(self) -> None:
        seat = make_seat(Table(view()))
        self.assertEqual(_label(seat.refuse_below, 0.05), "しょんぼり", "拒玩線落在「低落」裡面")
        self.assertLessEqual(seat.min_intensity, 0.12, "匯報線不高過「高喚醒」那條")

    def test_kick_prompt_keeps_the_game_rules(self) -> None:
        seat = make_seat(Table(view()))
        prompt = seat._prompt(view(turn={"seat": SEAT}), {"prompt": "急いで決めて"})
        self.assertIn("これはゲーム", prompt)
        self.assertIn("急いで決めて", prompt)

    def test_no_hp_field_no_hp_warning(self) -> None:
        seat = make_seat(Table(view()))
        self.assertNotIn("HP がかなり低い", seat._prompt(view(you={}), {}))
        self.assertIn("HP がかなり低い", seat._prompt(view(you={"hp": 2, "max_hp": 10}), {}))

    def test_mcp_handshake_is_retried_after_failure(self) -> None:
        mcp = TrpgMcp(TrpgConfig(table_id="t1", session="s1", seat=SEAT))
        calls = []

        def rpc(method, params):
            calls.append(method)
            if len(calls) == 1:
                raise OSError("hub down")
            return {"result": {"content": [{"type": "text", "text": "{}"}]}}

        mcp._rpc = rpc
        with self.assertRaises(OSError):
            mcp.ensure()
        mcp.ensure()
        self.assertEqual(calls, ["initialize", "initialize"], "第一次握手失敗要能再握一次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
