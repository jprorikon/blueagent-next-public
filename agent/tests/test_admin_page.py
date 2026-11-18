#!/usr/bin/env python3
"""腦端管理頁（owner 2026-11-09）：思考日誌／任務看板／帳號發行與外部帳號綁定。

判據：
  ① `/admin` 是靜態頁；資料 API 沒 token ⇒ 401、客人 ⇒ 403。
  ② 思考日誌把獨白、提前想、心情定位、私人日誌按時間合併（新的在前），附心跳此刻的判斷。
  ③ 外部帳號綁定寫進帳號表並**立刻生效**（綁上就認得、解除就不認）；已綁在別人身上的拒絕。
  ④ 發行客人帳號沿用 `/api/accounts`，新帳號的 App 綁定預設就是它自己。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403


class AdminCase(unittest.TestCase):
    def setUp(self) -> None:
        from agent.server import http as app_server

        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        app_server.set_state(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.owner = owner_headers(self.state)

    def call(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET",
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                kind = response.headers.get("Content-Type", "")
                return response.status, (json.loads(raw) if "json" in kind else raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw.decode("utf-8", "replace")

    def test_page_and_auth(self) -> None:
        status, page = self.call("/admin")
        self.assertEqual(status, 200)
        self.assertIn("BlueAgent 管理台", page)
        for path in ("/api/admin/thoughts?entity=mira", "/api/admin/identities"):
            self.assertEqual(self.call(path)[0], 401, path)
        status, issued = self.call("/api/accounts", {"role": "guest", "principal": "guest-x"}, self.owner)
        self.assertEqual(status, 201, issued)
        guest = auth_headers(issued["token"])
        self.assertEqual(self.call("/api/admin/identities", headers=guest)[0], 403, "客人不能進")
        for asset in ("/static/admin.js", "/static/admin.css"):
            self.assertEqual(self.call(asset)[0], 200, asset)

    def test_thoughts_merge_sources(self) -> None:
        import pathlib

        base = pathlib.Path(self.state.app.data_dir)
        (base / "inner").mkdir(parents=True, exist_ok=True)
        (base / "inner" / "mira.jsonl").write_text(json.dumps(
            {"at": time.time() - 60, "text": "観測表のこと考えてた", "action": "talk", "why": "見てほしい"},
            ensure_ascii=False) + "\n", encoding="utf-8")
        (base / "mood").mkdir(parents=True, exist_ok=True)
        (base / "mood" / "mira.json").write_text(json.dumps(
            {"read": "少し浮かれてる", "label": "わくわく", "at": time.time() - 30}, ensure_ascii=False),
            encoding="utf-8")
        self.state.app.activity_log.append("mira", kind="rest", visibility="private",
                                           text="なんだかだるくて、いまは何もしたくない")
        status, data = self.call("/api/admin/thoughts?entity=mira", headers=self.owner)
        self.assertEqual(status, 200, data)
        kinds = [row["kind"] for row in data["thoughts"]]
        self.assertIn("monologue", kinds)
        self.assertIn("mood", kinds)
        self.assertIn("private", kinds)
        stamps = [float(row.get("at") or 0) for row in data["thoughts"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True), "新的在前")
        monologue = next(row for row in data["thoughts"] if row["kind"] == "monologue")
        self.assertEqual((monologue["action"], monologue["why"]), ("talk", "見てほしい"))
        self.assertIn("ticks", data["now"], "心跳此刻的判斷")
        self.assertEqual(self.call("/api/admin/thoughts?entity=nobody", headers=self.owner)[0], 404)

    def test_link_and_unlink_take_effect(self) -> None:
        status, issued = self.call("/api/accounts", {"role": "guest", "principal": "guest-kiseki",
                                                     "display": "輝石"}, self.owner)
        self.assertEqual(status, 201)
        account = issued["account"]["account_id"]
        status, data = self.call("/api/admin/identities", headers=self.owner)
        row = next(item for item in data["accounts"] if item["account_id"] == account)
        self.assertEqual(row["links"]["app"], [account], "新帳號的 App 綁定預設就是它自己")
        principals = self.state.app.principals
        status, data = self.call("/api/admin/identities/link", {"account_id": account, "source": "discord",
                                                                "external_id": "998877"}, self.owner)
        self.assertEqual(status, 200, data)
        self.assertEqual(principals.lookup("discord", "998877"), "guest-kiseki", "綁上就認得")
        status, clash = self.call("/api/admin/identities/link", {"account_id": "acct_owner", "source": "discord",
                                                                 "external_id": "998877"}, self.owner)
        self.assertEqual((status, clash.get("error")), (409, "already_linked"), "已綁在別人身上")
        status, data = self.call("/api/admin/identities/unlink", {"account_id": account, "source": "discord",
                                                                  "external_id": "998877"}, self.owner)
        self.assertEqual(status, 200, data)
        self.assertEqual(principals.lookup("discord", "998877"), "", "解除就不認")
        self.assertEqual(self.call("/api/admin/identities/link", {"account_id": account, "source": "fax",
                                                                  "external_id": "1"}, self.owner)[0], 400)

    def test_aem_view_has_now_series_events_and_cause(self) -> None:
        """AEM 可視化：此刻／曲線（關閉式投影）／事件／原因／出口。"""
        affect = self.state.app.affect
        affect.note_message("mira", "観測表できたよ", speaker="owner")
        affect.note_state("mira", {"emotion": {"label": "嬉", "intensity": 0.9, "valence": 0.8}},
                          source_event_id="ev-aem-1")
        status, data = self.call("/api/admin/aem?entity=mira&hours=24&step=300&events=10",
                                 headers=self.owner)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["available"])
        now_row = data["now"]
        for key in ("label", "valence", "arousal", "social_need", "propensity"):
            self.assertIn(key, now_row)
        series = data["series"]
        self.assertGreater(len(series), 10, "24 小時 / 5 分鐘該有近 288 點")
        stamps = [row["at"] for row in series]
        self.assertEqual(stamps, sorted(stamps), "時間軸要往前走")
        self.assertAlmostEqual(stamps[-1], data["generated_at"], delta=1.0)
        # 曲線最後一點就是「此刻」（同一套關閉式投影，不是另一份取樣）
        self.assertAlmostEqual(float(series[-1]["valence"]), float(now_row["valence"] or 0), places=3)
        self.assertTrue(data["events"], "剛寫進去的事件要看得到")
        row = data["events"][0]
        self.assertEqual(row["cause"], "owner", "主人那一句引起的 ⇒ 主人看得見")
        self.assertFalse(row["hidden"])
        self.assertTrue(data["cause"].get("key"))
        self.assertIn("heartbeat", data, "出口那一格：為什麼沒開口")
        # 曲線要反映事件：事件之前低、之後高
        cut = float(row["at"]) - 1.0
        before = [point for point in series if point["at"] <= cut]
        if before:
            self.assertGreater(float(series[-1]["valence"]), float(before[-1]["valence"]))

    def test_aem_requires_owner_and_known_entity(self) -> None:
        self.assertEqual(self.call("/api/admin/aem?entity=mira")[0], 401, "沒 token")
        status, issued = self.call("/api/accounts", {"role": "guest", "principal": "guest-aem"}, self.owner)
        self.assertEqual(status, 201, issued)
        guest = auth_headers(issued["token"])
        self.assertEqual(self.call("/api/admin/aem?entity=mira", headers=guest)[0], 403, "客人不能看")
        self.assertEqual(self.call("/api/admin/aem?entity=nobody", headers=self.owner)[0], 404)

    def test_aem_series_projects_past_moments(self) -> None:
        """曲線是重算出來的真值：同一筆事件，事件後的取樣一定高於事件前。"""
        affect = self.state.app.affect
        now = time.time()
        affect.note_message("mira", "今日はいい日だった", speaker="owner")
        affect.note_state("mira", {"emotion": {"label": "嬉", "intensity": 0.9, "valence": 0.9}},
                          source_event_id="ev-aem-2", occurred_at=now - 3600)
        points = affect.series("mira", hours=2, step=300, at=now)
        self.assertGreater(len(points), 10)
        early = [point for point in points if point["at"] <= now - 3660]
        late = [point for point in points if point["at"] >= now - 300]
        self.assertTrue(early and late)
        self.assertGreater(late[-1]["valence"], early[-1]["valence"],
                           "事件之後的投影要比之前高（漏積分＋衰減）")

    def test_board_cards_show_origin(self) -> None:
        ledger = self.state.app.ledgers["aster"]
        ledger.add("試す", origin="self", because="オーナーが言っていた")
        status, data = self.call("/api/tasks?entity=aster&view=board", headers=self.owner)
        self.assertEqual(status, 200)
        cards = [card for rows in data["columns"].values() for card in rows]
        mine = next(card for card in cards if card["title"] == "試す")
        self.assertEqual((mine["origin"], mine["because"]), ("self", "オーナーが言っていた"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
