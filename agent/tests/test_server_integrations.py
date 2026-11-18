#!/usr/bin/env python3
"""啟動裝配拆段（review.md 2026-10-25 P1：`server/main.main()` 347 行）。

判據：
  ① 沒開 Discord ⇒ 不載入 TRPG 席位與 Discord 信道的實作（延遲 import）；
  ② TRPG 的回呼可以單獨測：說話（done 沒全文時從 frames 還原）、匯報（local／dm／discord）、
     情緒強度、遊戲情緒（記在桌上不記在主人頭上）、離席與「不想玩」通知走 delivery；
  ③ 打斷：停播、記情緒、記遙測；告警：私聊送不出才貼頻道；信道：Discord 與語音都註冊；
  ④ `main()` 只排順序（≤40 行），各段是有名字的函式。
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.server.integrations import alerts, delivery, discord, trpg

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"floor_holder": "mira"} if name == "interrupt" else True
        return record


def _state(**app_fields):
    app = types.SimpleNamespace(**app_fields)
    return types.SimpleNamespace(app=app)


CONFIG = types.SimpleNamespace(entity="mira", table_id="table-1", seat="p5", kick_token="")


class TestDiscordOffLoadsNothing(unittest.TestCase):
    def test_no_trpg_or_discord_delivery_without_token(self):
        from agent.server import main as server_main
        for name in ("agent.adapter.trpg", "agent.server.integrations.trpg",
                     "agent.server.integrations.alerts", "agent.server.integrations.delivery"):
            sys.modules.pop(name, None)
        with mock.patch("agent.server.integrations.discord.start_discord", return_value=None):
            server_main.start_integrations(_state())
        for name in ("agent.adapter.trpg", "agent.server.integrations.trpg",
                     "agent.server.integrations.alerts", "agent.server.integrations.delivery"):
            self.assertNotIn(name, sys.modules, name)

    def test_discord_on_wires_trpg_alerts_channels_in_order(self):
        from agent.server import main as server_main
        order = []
        source = types.SimpleNamespace(config=types.SimpleNamespace(
            guild_id="g", channel_id="c", owner_ids={"1"}))
        with mock.patch("agent.server.integrations.discord.start_discord", return_value=source), \
                mock.patch("agent.server.integrations.trpg.start_trpg",
                           side_effect=lambda *a: order.append("trpg")), \
                mock.patch("agent.server.integrations.alerts.start_alerts",
                           side_effect=lambda *a: order.append("alerts")), \
                mock.patch("agent.server.integrations.delivery.register_discord_channels",
                           side_effect=lambda *a: order.append("channels")):
            server_main.start_integrations(_state())
        self.assertEqual(order, ["trpg", "alerts", "channels"], "跟拆之前同一個順序")

    def test_build_discord_without_token_returns_none(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": ""}):
            self.assertIsNone(discord.start_discord(make_state()))


class TestTrpgCallbacks(unittest.TestCase):
    def test_speaker_recovers_text_from_frames(self):
        sent = {}

        class State:
            app = types.SimpleNamespace()

            def send(self, prompt, **kwargs):
                sent.update(kwargs)
                return {"ok": True, "frames": [{"kind": "delta", "payload": {"text": "よし、"}},
                                               {"kind": "delta", "payload": {"text": "動くよ"}}]}

            def reply_text(self, result):
                return ""

        text = trpg.make_speaker(State(), CONFIG)("盤面")
        self.assertEqual(text, "よし、動くよ")
        self.assertEqual(sent["source_override"], "trpg:table-1", "一定要是 trpg:<table>")
        self.assertEqual(sent["principal"], "owner")

    def test_reporter_writes_local_and_private_log(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-trpg-"))
        log = _Recorder()
        state = _state(data_dir=root, activity_log=log)
        source = _Recorder()
        self.assertTrue(trpg.make_reporter(state, CONFIG, source, "local")("三ターン目", {"round": 3}))
        self.assertEqual(log.calls[0][2]["visibility"], "private")
        self.assertEqual(source.calls, [], "local 不碰 Discord")
        self.assertTrue(any(root.rglob("*")), "本地匯報檔要寫出來")
        trpg.make_reporter(state, CONFIG, source, "dm")("x", {})
        self.assertEqual(source.calls[-1][0], "send_dm")
        trpg.make_reporter(state, CONFIG, source, "discord")("x", {})
        self.assertEqual(source.calls[-1][0], "send_text")
        self.assertFalse(trpg.make_reporter(state, CONFIG, source, "local")("  ", {}))

    def test_intensity_and_game_affect(self):
        affect = mock.Mock()
        affect.snapshot.return_value = {"affect": {"valence": -0.7, "arousal": 0.4}}
        state = _state(affect=affect)
        self.assertAlmostEqual(trpg.make_intensity(state, CONFIG)(), 0.7)
        trpg.make_game_affect(state, CONFIG)("damage", -0.2, 0.3, {"seq": 9})
        # 2026-11-09：走 outcome（她自己這邊的事）；以前 appraise(target=game) 每筆被核心拒（INVALID_INPUT）
        kwargs = affect.outcome.call_args.kwargs
        self.assertEqual((kwargs["kind"], kwargs["target_type"]), ("concern", "task"))
        affect.appraise.assert_not_called()
        affect.outcome.reset_mock()
        trpg.make_game_affect(state, CONFIG)("unknown_event", 0, 0, {})
        affect.outcome.assert_not_called()
        self.assertEqual(trpg.make_intensity(_state(), CONFIG)(), 0.0)

    def test_notifiers_go_through_delivery(self):
        router = mock.Mock()
        state = _state(delivery=router)
        trpg.make_table_closed_notifier(state, CONFIG)("ended")
        trpg.make_master_notice(state, CONFIG)("今日はやめとく")
        kinds = [call.args[1]["kind"] for call in router.send.call_args_list]
        self.assertEqual(kinds, ["trpg_closed", "proactive"])
        self.assertTrue(all(call.args[0] == "owner" for call in router.send.call_args_list))


class TestOtherIntegrations(unittest.TestCase):
    def test_barge_in_stops_voice_and_records(self):
        voice, affect, telemetry = _Recorder(), _Recorder(), _Recorder()
        state = _state(voice=voice, affect=affect, telemetry=telemetry)
        discord.make_barge_in_handler(state, "mira")("u1", 4)
        self.assertEqual(voice.calls[0][0], "interrupt")
        self.assertEqual(affect.calls[0][0], "interrupted")
        self.assertEqual(telemetry.calls[0][2]["event"], "barge_in")

    def test_alert_sender_falls_back_to_channel(self):
        source = mock.Mock()
        source.send_dm.return_value = False
        source.send_text.return_value = True
        self.assertTrue(alerts.make_owner_sender(source)("p95 高"))
        source.send_text.assert_called_once()

    def test_discord_channels_are_registered(self):
        router = types.SimpleNamespace(channels={}, order=["app"])
        state = _state(delivery=router, voice=object())
        delivery.register_discord_channels(state, mock.Mock())
        self.assertEqual(router.order, ["app", "discord", "voice"])
        self.assertEqual(sorted(router.channels), ["discord", "voice"])


class TestMainOnlyOrders(unittest.TestCase):
    def test_main_is_short(self):
        source = (ROOT / "agent" / "server" / "main.py").read_text(encoding="utf-8")
        main = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        self.assertLessEqual(main.end_lineno - main.lineno + 1, 40)
        nested = [node.name for node in ast.walk(main)
                  if isinstance(node, ast.FunctionDef) and node is not main]
        self.assertEqual(nested, [], "main() 裡不要再長閉包")


if __name__ == "__main__":
    unittest.main(verbosity=2)
