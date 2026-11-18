"""Agent 公开活动 → App Server 公共区的显式发布（2026-11-07：切换后角色一条帖子都没有）。"""

from __future__ import annotations

import json
import pathlib
import tempfile
import time
import unittest

from agent.adapter.app_board_publish import (PublicActivityPublisher, build_board_publisher,
                                       clean_post_body, compose_post, finished_body,
                                       mutation_id, publishable)
from agent.adapter.app_protocol import BoardError

NOW = 1_794_000_000.0


def _row(entity, activity_id, step, text, *, at, visibility="public", kind="study",
         activity="観測ノート"):
    return {"at": at, "entity": entity, "kind": kind, "activity": activity,
            "activity_id": activity_id, "step": step, "visibility": visibility, "text": text}


class _Client:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = list(fail or [])

    def board_call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.fail:
            error = self.fail.pop(0)
            if error is not None:
                raise error
        return {"post": {"id": "pst_" + str(len(self.calls))}}


class TestAppBoardPublish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()

    def _write(self, entity, rows, name=None):
        path = self.logs / (name or f"activity-{entity}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _publisher(self, clients, **kwargs):
        return PublicActivityPublisher(self.logs, clients, self.root / "state.json",
                                       clock=lambda: NOW, log=lambda _m: None, **kwargs)

    def test_finished_body_only_for_the_closing_step(self):
        self.assertEqual(finished_body("アスター「観測」3/3 完了：境界が見えた。"), "境界が見えた。")
        self.assertEqual(finished_body("アスター「観測」完了：一回で済んだ。"), "一回で済んだ。")
        self.assertEqual(finished_body("アスター「観測」1/3 調べる：途中。"), "")
        self.assertEqual(finished_body(""), "")

    def test_only_finished_public_activities_are_publishable(self):
        self._write("aster", [
            _row("aster", "act_a", 0, "アスター「観測」1/2 調べる：途中", at=NOW - 600),
            _row("aster", "act_a", 1, "アスター「観測」2/2 完了：境界が見えた。", at=NOW - 500),
            # 还没做完
            _row("aster", "act_b", 0, "アスター「整理」1/3 集める：途中", at=NOW - 400),
            # 最后一步是 private ⇒ 整件不公开
            _row("aster", "act_c", 0, "アスター「日記」1/2 書く：公開部分", at=NOW - 300),
            _row("aster", "act_c", 1, "アスター「日記」2/2 完了：秘密", at=NOW - 200, visibility="private"),
            # 对人说的话不是帖子
            {"at": NOW - 100, "entity": "aster", "kind": "heartbeat", "activity": "",
             "activity_id": "", "step": 0, "visibility": "public", "text": "オーナー、起きてる？"},
            # 回看窗口之外
            _row("aster", "act_old", 0, "アスター「昔」完了：古い", at=NOW - 10 * 86400),
        ])
        items = publishable(self.logs, "aster", NOW - 3 * 86400)
        self.assertEqual([item["source_ref"] for item in items], ["activity:aster:act_a"])
        self.assertEqual(items[0]["body"], "【観測ノート】\n境界が見えた。",
                         "帖头是活动名（owner 2026-11-08「没头没尾」）")

    def test_rotated_daily_files_are_read_within_lookback(self):
        day = time.strftime("%Y%m%d", time.localtime(NOW - 86400))
        self._write("nori", [_row("nori", "act_d", 0, "ノリ「遊ぶ」完了：楽しかった", at=NOW - 86400)],
                    name=f"daily/activity-nori-{day}.jsonl")
        items = publishable(self.logs, "nori", NOW - 3 * 86400)
        self.assertEqual([item["source_ref"] for item in items], ["activity:nori:act_d"])

    def test_sweep_posts_once_with_stable_idempotency(self):
        self._write("aster", [_row("aster", "act_a", 0, "アスター「観測」完了：見えた", at=NOW - 60)])
        client = _Client()
        publisher = self._publisher({"aster": client})
        self.assertEqual(publisher.sweep()["posted"], 1)
        name, args = client.calls[0]
        self.assertEqual(name, "post")
        self.assertEqual(args["source_ref"], "activity:aster:act_a")
        self.assertEqual(args["client_mutation_id"], mutation_id("activity:aster:act_a"))
        self.assertEqual(args["media"], [])
        # 同一进程、以及重启后的新实例都不再打服务端
        self.assertEqual(publisher.sweep()["skipped"], 1)
        again = self._publisher({"aster": client})
        self.assertEqual(again.sweep()["skipped"], 1)
        self.assertEqual(len(client.calls), 1)

    def test_migrated_projection_conflict_is_settled_not_retried(self):
        self._write("aster", [_row("aster", "act_a", 0, "アスター「観測」完了：見えた", at=NOW - 60)])
        client = _Client(fail=[BoardError(409, "source_ref_conflict")])
        publisher = self._publisher({"aster": client})
        self.assertEqual(publisher.sweep()["settled"], 1)
        self.assertEqual(publisher.sweep()["skipped"], 1)
        self.assertEqual(len(client.calls), 1)

    def test_rate_limit_stops_that_entity_and_retries_next_sweep(self):
        self._write("aster", [_row("aster", "act_a", 0, "アスター「一」完了：一", at=NOW - 90),
                            _row("aster", "act_b", 0, "アスター「二」完了：二", at=NOW - 60)])
        self._write("nori", [_row("nori", "act_k", 0, "ノリ「三」完了：三", at=NOW - 30)])
        aster = _Client(fail=[BoardError(429, "rate_limited")])
        nori = _Client()
        publisher = self._publisher({"aster": aster, "nori": nori})
        first = publisher.sweep()
        self.assertEqual((first["posted"], first["failed"]), (1, 1))   # nori 照发，aster 这一轮停
        self.assertEqual(len(aster.calls), 1)
        second = publisher.sweep()
        self.assertEqual(second["posted"], 2)
        self.assertEqual([args["source_ref"] for _n, args in aster.calls],
                         ["activity:aster:act_a", "activity:aster:act_a", "activity:aster:act_b"])

    def test_transport_failure_is_retried(self):
        self._write("aster", [_row("aster", "act_a", 0, "アスター「観測」完了：見えた", at=NOW - 60)])
        client = _Client(fail=[RuntimeError("app_server_http_502")])
        publisher = self._publisher({"aster": client})
        self.assertEqual(publisher.sweep()["failed"], 1)
        self.assertEqual(publisher.sweep()["posted"], 1)

    def test_long_body_is_cut_to_board_limit(self):
        self._write("aster", [_row("aster", "act_a", 0, "アスター「長い」完了：" + "あ" * 2500, at=NOW - 60)])
        body = publishable(self.logs, "aster", NOW - 86400)[0]["body"]
        self.assertEqual(len(body), 2000)
        self.assertTrue(body.endswith("…"))

    def test_autopublish_is_off_by_default_and_needs_an_explicit_switch(self):
        """owner 2026-11-08：「发帖这个事情和事情收束不收束应该解耦掉」⇒ 自动发默认关。"""
        state = type("S", (), {"app": type("A", (), {"data_dir": str(self.root)})()})()
        env = {"BLUEAGENT_APP_SERVER_URL": "http://app:8790",
               "BLUEAGENT_APP_SERVER_TOKENS": "aster=t1,nori=t2"}
        self.assertIsNone(build_board_publisher(state, env), "默认不自动发帖")
        legacy = build_board_publisher(state, {**env, "BLUEAGENT_BOARD_AUTOPUBLISH": "1"})
        self.assertIsNotNone(legacy, "显式打开才回到旧行为")
        self.assertEqual(sorted(legacy.clients), ["aster", "nori"])
        self.assertIsNone(build_board_publisher(state, {"BLUEAGENT_APP_SERVER_URL": "http://app",
                                                        "BLUEAGENT_BOARD_AUTOPUBLISH": "1"}))

    def test_life_step_log_line_is_recognised_as_closing(self):
        """發布器認收尾靠 life_step 寫日誌的格式；改那邊的格式這裡要一起改。"""
        source = (pathlib.Path(__file__).resolve().parents[2] / "agent/brain/runtime/life_step.py") \
            .read_text(encoding="utf-8")
        self.assertIn('head = f"{name}「{activity.title}」{counter}"', source)
        self.assertIn('f"{head}完了：{body}" if last', source)

    def test_app_state_starts_and_closes_publisher(self):
        source = (pathlib.Path(__file__).resolve().parents[2] / "agent/brain/compose/state.py") \
            .read_text(encoding="utf-8")
        self.assertIn("build_board_publisher(self)", source)
        self.assertIn("self.board_publisher.start()", source)
        self.assertIn("self.board_publisher.close()", source)

    def test_intermediate_step_with_an_inner_finish_phrase_is_not_a_closing_step(self):
        """⑥ REVIEW 2026-11-07 #6：`^.*?` 会回溯越过步骤标头 ⇒ 中间步被当收尾提前发出。"""
        self.assertEqual(
            finished_body("アスター「整理」2/3 まとめる：「下書き」完了：次は清書"), "")
        self.assertEqual(
            finished_body("ミラ「観測」1/3 下書き：「要約」完了：まだ途中"), "")
        # 真正的收尾仍然认（角色名里不许出现「或：）
        self.assertEqual(finished_body("アスター「整理」3/3 完了：清書まで終わった。"),
                         "清書まで終わった。")

    def test_post_body_gets_a_headline_and_loses_markdown(self):
        """owner 2026-11-08：「没头没尾」。帖头放活动名；成品步里的表格清成纯文本。"""
        body = compose_post("記録のしかたを調べる",
                            "###観測表|日時|活動|\n|---|---|\n|11/8 00:19|調べる|\n\n"
                            "事実と気づきを分けて書き直した。")
        self.assertTrue(body.startswith("【記録のしかたを調べる】"), body)
        self.assertNotIn("#", body)
        self.assertNotIn("|---", body)
        self.assertIn("・11/8 00:19、調べる", body)
        self.assertIn("事実と気づきを分けて書き直した。", body.splitlines()[-1])

    def test_clean_body_keeps_plain_text_untouched(self):
        self.assertEqual(clean_post_body("普通の文。二文目。"), "普通の文。二文目。")

    def test_publish_body_uses_the_headline(self):
        self._write("aster", [_row("aster", "act_h", 3, "アスター「観測」3/3 完了：境界が見えた。",
                                 at=NOW - 60)])
        client = _Client()
        self._publisher({"aster": client}).sweep()
        self.assertTrue(client.calls[0][1]["text"].startswith("【観測ノート】"),
                        client.calls[0][1]["text"])

    def test_publish_carries_the_activity_time_not_the_publish_time(self):
        """⑦ 帖子时间＝活动真正做完的时刻（服务端夹在回看窗口内）。"""
        at = NOW - 5400.0
        self._write("aster", [_row("aster", "act_1", 3, "アスター「観測」3/3 完了：境界が見えた。",
                                 at=at)])
        client = _Client()
        self._publisher({"aster": client}).sweep()
        self.assertEqual(client.calls[0][0], "post")
        self.assertEqual(client.calls[0][1]["occurred_at"], at)


if __name__ == "__main__":
    unittest.main()
