#!/usr/bin/env python3
"""多設備配對＋Discord 源級認證（owner 2026-11-16）。

判據（配對）：
  ① 已登入端末開碼 ⇒ 新端末用 6 位數字換到自己的 token，認出來是同一個帳號、另一台端末；原 token 照用；
  ② 碼只能用一次、5 分鐘過期、同帳號再開一次舊碼作廢；猜碼按 IP 節流；
  ③ 客人也可以多設備；服務帳號不能配對；
  ④ 端末清單標出「這一台」；撤銷一台只踢那一台；撤銷 primary 連 auth.json 的舊路徑也進不來，輪替後恢復；
  ⑤ 落地頁不消耗碼；App Server 的內部詢問（kick token）認得端末 token，撤銷後就不認、服務帳號不認。
判據（服務帳號）：
  ⑥ owner token 帶 `X-Source`／`X-External-Id` 不再能冒充任何 Discord 用戶；
  ⑦ 每顆 bot 一把：只能替自己的源、自己的角色、已綁定的外部身分發回合；別的路徑一律 401；
  ⑧ 服務帳號不登記成 principal。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

import agent.server.api as api
from agent.brain.domain.auth import Role
from agent.brain.domain.principal import Principal
from agent.brain.runtime.identity.accounts import hash_token
from agent.brain.runtime.identity.pairing import PairingStore
from agent.server.api import pairing
from agent.server.api.turns import prepare_send


class Base(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.auth = self.state.app.auth
        self.accounts = self.state.app.accounts
        self.owner_token = self.auth.owner_token
        self.owner = self.auth.authenticate(f"Bearer {self.owner_token}", path="/api/me")

    def pair(self, who, name="Pixel 9"):
        status, started = pairing.post_pair_start(self.state, {}, auth=who)
        self.assertEqual(status, 200)
        status, claimed = pairing.post_pair_claim(self.state, {"code": started["code"], "name": name},
                                                  ip="10.0.0.2")
        self.assertEqual(status, 200, claimed)
        return claimed


class TestPairing(Base):
    def test_new_device_gets_its_own_token_for_the_same_account(self):
        claimed = self.pair(self.owner)
        ctx = self.auth.authenticate(f"Bearer {claimed['token']}", path="/api/me")
        self.assertEqual(ctx.role, Role.OWNER)
        self.assertEqual(ctx.token_id, "acct_owner")
        self.assertTrue(ctx.device.startswith("dev_"))
        self.assertNotEqual(claimed["token"], self.owner_token)
        self.assertEqual(self.auth.authenticate(f"Bearer {self.owner_token}").device, "primary",
                         "原來那台照用")
        self.assertNotIn(claimed["token"], (self.state.app.accounts.path).read_text(encoding="utf-8"),
                         "端末 token 也只存雜湊")
        self.assertEqual(api.get_me(self.state, auth=ctx)[1]["device"], ctx.device)

    def test_code_is_single_use(self):
        _, started = pairing.post_pair_start(self.state, {}, auth=self.owner)
        self.assertEqual(pairing.post_pair_claim(self.state, {"code": started["code"]}, ip="a")[0], 200)
        self.assertEqual(pairing.post_pair_claim(self.state, {"code": started["code"]}, ip="a")[0], 404)
        self.assertEqual(pairing.post_pair_claim(self.state, {"code": "12345"}, ip="a")[0], 404)

    def test_guesses_are_rate_limited_per_ip(self):
        codes = [pairing.post_pair_claim(self.state, {"code": f"{n:06d}"}, ip="9.9.9.9")[0]
                 for n in range(pairing.CLAIM_PER_MINUTE + 1)]
        self.assertEqual(codes[-1], 429)
        self.assertNotEqual(pairing.post_pair_claim(self.state, {"code": "000000"}, ip="8.8.8.8")[0], 429,
                            "別的 IP 不受影響")

    def test_guest_can_have_several_devices(self):
        _, token = self.accounts.issue(role="guest", principal="alice")
        guest = self.auth.authenticate(f"Bearer {token}", path="/api/me")
        claimed = self.pair(guest, "iPad")
        ctx = self.auth.authenticate(f"Bearer {claimed['token']}", path="/api/me")
        self.assertEqual((ctx.role, ctx.principal), (Role.GUEST, "alice"))

    def test_device_list_and_revoke_one(self):
        first = self.pair(self.owner, "Pixel")
        second = self.pair(self.owner, "Tablet")
        me = self.auth.authenticate(f"Bearer {first['token']}", path="/api/devices")
        _, listed = pairing.get_devices(self.state, auth=me)
        names = {row["name"]: row for row in listed["devices"]}
        self.assertTrue(names["Pixel"]["current"])
        self.assertFalse(names["Tablet"]["current"])
        self.assertIn("primary", [row["device_id"] for row in listed["devices"]])
        status, _ = pairing.post_device_revoke(self.state, {"device_id": second["device"]["device_id"]},
                                               auth=me)
        self.assertEqual(status, 200)
        self.assertIsNone(self.auth.authenticate(f"Bearer {second['token']}"))
        self.assertIsNotNone(self.auth.authenticate(f"Bearer {first['token']}"))
        self.assertIsNotNone(self.auth.authenticate(f"Bearer {self.owner_token}"))

    def test_revoking_primary_also_closes_the_legacy_owner_path(self):
        claimed = self.pair(self.owner)
        me = self.auth.authenticate(f"Bearer {claimed['token']}", path="/api/devices")
        self.assertEqual(pairing.post_device_revoke(self.state, {"device_id": "primary"}, auth=me)[0], 200)
        self.assertIsNone(self.auth.authenticate(f"Bearer {self.owner_token}"),
                          "撤掉 primary ⇒ auth.json 的舊雜湊也不能把它救回來")
        self.assertIsNotNone(self.auth.authenticate(f"Bearer {claimed['token']}"))
        _, new = self.accounts.rotate("acct_owner")
        self.assertIsNotNone(self.accounts.verify(new), "輪替出新的 primary 就有效")

    def test_service_accounts_cannot_pair(self):
        _, token = self.accounts.issue(role="service", principal="svc-discord-aster",
                                       source="discord", entity="aster")
        bot = self.auth.authenticate(f"Bearer {token}", path="/api/turn")
        self.assertEqual(pairing.post_pair_start(self.state, {}, auth=bot)[0], 403)
        self.assertEqual(pairing.post_pair_start(self.state, {}, auth=None)[0], 401)

    def test_landing_page_does_not_consume_the_code(self):
        _, started = pairing.post_pair_start(self.state, {"base_url": "https://box.example:8443"},
                                             auth=self.owner)
        self.assertEqual(started["url"], f"https://box.example:8443/pair/{started['code']}")
        status, page = pairing.pair_page(self.state, started["code"])
        self.assertEqual(status, 200)
        self.assertIn(f"blueagent://pair?code={started['code']}", page)
        self.assertEqual(pairing.post_pair_claim(self.state, {"code": started["code"]}, ip="a")[0], 200)
        self.assertEqual(pairing.pair_page(self.state, started["code"])[0], 404)

    def test_qr_is_a_png_when_segno_is_installed(self):
        try:
            import segno  # noqa: F401
        except ImportError:
            self.skipTest("segno 沒裝（image 裡有；本機跳過）")
        _, started = pairing.post_pair_start(self.state, {"base_url": "https://box.example:8443"},
                                             auth=self.owner)
        self.assertTrue(started["qr"].startswith("data:image/png;base64,"))

    def test_bad_base_url_gives_no_qr(self):
        _, started = pairing.post_pair_start(self.state, {"base_url": "javascript:alert(1)"},
                                             auth=self.owner)
        self.assertEqual((started["url"], started["qr"]), ("", ""))


class TestPairingStore(unittest.TestCase):
    def test_codes_expire_and_restarting_replaces_the_old_one(self):
        now = [1000.0]
        store = PairingStore(clock=lambda: now[0])
        first = store.start("acct_a")["code"]
        second = store.start("acct_a")["code"]
        self.assertEqual(store.claim(first), "" if first != second else "acct_a")
        third = store.start("acct_b")["code"]
        now[0] += 301
        self.assertEqual(store.claim(third), "", "5 分鐘過期")


class TestIntrospection(Base):
    def ask(self, digest, token="kick-secret"):
        with mock.patch.dict(os.environ, {"BLUEAGENT_APP_KICK_TOKEN": "kick-secret"}):
            return api.app_kick_handle(self.state, token, {"token_sha256": digest}, "/api/app/introspect")

    def test_device_tokens_are_known_until_revoked(self):
        claimed = self.pair(self.owner)
        status, found = self.ask(hash_token(claimed["token"]))
        self.assertEqual(status, 200)
        self.assertEqual((found["valid"], found["account_id"]), (True, "acct_owner"))
        self.accounts.revoke_device("acct_owner", claimed["device"]["device_id"])
        self.assertFalse(self.ask(hash_token(claimed["token"]))[1]["valid"])

    def test_needs_the_internal_token_and_ignores_service_accounts(self):
        self.assertEqual(self.ask(hash_token(self.owner_token), token="wrong")[0], 403)
        _, token = self.accounts.issue(role="service", principal="svc-discord-aster",
                                       source="discord", entity="aster")
        self.assertFalse(self.ask(hash_token(token))[1]["valid"])


class TestServiceTokens(Base):
    def setUp(self):
        super().setUp()
        self.state.app.principals.register(Principal("guest-alice", "Alice"), replace=True)
        self.state.app.principals.bind("discord", "4242", "guest-alice")
        _, self.bot_token = self.accounts.issue(role="service", principal="svc-discord-aster",
                                                source="discord", entity="aster")
        self.bot = self.auth.authenticate(f"Bearer {self.bot_token}", path="/api/turn")

    def test_owner_token_can_no_longer_claim_a_discord_identity(self):
        self.assertEqual(self.auth.resolve_principal(self.owner, source="discord", external_id="4242"),
                         "owner")

    def test_bot_resolves_only_bound_ids_on_its_own_source(self):
        resolve = self.auth.resolve_principal
        self.assertEqual(self.bot.role, Role.SERVICE)
        self.assertEqual(resolve(self.bot, source="discord", external_id="4242"), "guest-alice")
        self.assertEqual(resolve(self.bot, source="discord", external_id="9999"), "")
        self.assertEqual(resolve(self.bot, source="app", external_id="4242"), "")
        self.assertEqual(resolve(self.bot, "owner", source="", external_id=""), "", "不能自報 principal")

    def test_bot_turns_are_bound_to_its_entity_and_known_ids(self):
        body = {"text": "やあ", "entity": "aster"}
        self.assertEqual(prepare_send(self.state, body, auth=self.bot, source="discord",
                                      external_id="9999")[1], {"error": "unknown_external_id"})
        self.assertEqual(prepare_send(self.state, dict(body, entity="nori"), auth=self.bot,
                                      source="discord", external_id="4242")[1],
                         {"error": "entity_not_bound"})

    def test_bot_token_only_works_on_turn_paths(self):
        self.assertIsNone(self.auth.authenticate(f"Bearer {self.bot_token}", path="/api/me"))
        self.assertIsNone(self.auth.authenticate(f"Bearer {self.bot_token}", path="/api/accounts"))
        self.assertIsNotNone(self.auth.authenticate(f"Bearer {self.bot_token}", path="/api/audio"))

    def test_owner_issues_one_token_per_bot(self):
        status, made = api.create_account(self.state, {"role": "service", "source": "discord",
                                                       "entity": "nori"}, auth=self.owner)
        self.assertEqual(status, 201)
        self.assertEqual((made["account"]["source"], made["account"]["entity"]), ("discord", "nori"))
        bot = self.auth.authenticate(f"Bearer {made['token']}", path="/api/turn")
        self.assertEqual((bot.role, bot.entity, bot.principal), (Role.SERVICE, "nori", "svc-discord-nori"))
        self.assertEqual(api.create_account(self.state, {"role": "service", "source": "line",
                                                         "entity": "nori"}, auth=self.owner)[0], 400)
        self.assertEqual(api.create_account(self.state, {"role": "service", "source": "discord",
                                                         "entity": "nori"}, auth=self.bot)[0], 403)

    def test_bots_are_not_registered_as_people(self):
        from agent.brain.compose.identity import bind_identities
        bind_identities(self.state.app, env={})
        self.assertIsNone(self.state.app.principals.get("svc-discord-aster"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
