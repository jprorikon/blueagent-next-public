#!/usr/bin/env python3
"""App Server 的人類 token 以 Agent 為準（多設備配對，owner 2026-11-16）。

判據：
  ① Agent 說是誰就是誰：配對出來的新端末（靜態表裡沒有）也能進 `/v1/*`；
  ② Agent 說無效 ⇒ 拒絕，就算靜態表還認得（被撤銷的 primary）；
  ③ Agent 連不上 ⇒ 看這把上次的結論（審核 R1，2026-11-17）：上次無效 ⇒ 拒；上次有效且在寬限（10 分鐘）內 ⇒ 放行；
     沒問過／超過寬限 ⇒ 拒——**被撤銷的 primary 不會因為 Agent 掉線又能用**；
     沒設定 kick token（introspection 沒開）⇒ 照舊看靜態表；角色帳號不問 Agent、網路分區也照常；
  ④ 同一把 60 秒內只問一次；只送雜湊、帶 kick token；
  ⑤ 常駐 SSE 每 30 秒重驗走同一條 `authenticate()` ⇒ 規則一樣。
"""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import tempfile
import unittest

import app_paths  # noqa: F401

from app.server.accounts import AccountStore
from app.server.device_auth import Introspector


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class FakeAgent:
    def __init__(self, answers: dict[str, str | None], *, down: bool = False) -> None:
        self.answers = answers
        self.down = down
        self.asked: list[dict] = []

    def __call__(self, request, timeout=0):
        if self.down:
            raise OSError("connection refused")
        body = json.loads(request.data.decode("utf-8"))
        self.asked.append({"body": body, "token": request.get_header("X-app-kick-token")})
        account = self.answers.get(body["token_sha256"])
        payload = {"valid": True, "account_id": account} if account else {"valid": False}
        return io.BytesIO(json.dumps(payload).encode("utf-8"))


class DeviceAuthCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="ba-app-devauth-")
        self.addCleanup(folder.cleanup)
        self.path = pathlib.Path(folder.name) / "accounts.json"
        self.path.write_text(json.dumps({"accounts": [
            {"id": "acct_owner", "kind": "owner", "display": "主人", "token_sha256": digest("primary")},
            {"id": "acct_g", "kind": "guest", "display": "客人", "token_sha256": digest("guest")},
            {"id": "aster", "kind": "entity", "display": "アスター", "token_sha256": digest("aster-token")},
        ]}), encoding="utf-8")
        self.now = [0.0]

    def store(self, agent, *, grace_s: float = 600.0) -> AccountStore:
        return AccountStore(self.path, introspector=Introspector(
            "http://agent:18791", "kick", clock=lambda: self.now[0], opener=agent, grace_s=grace_s))

    def test_paired_device_is_accepted(self):
        agent = FakeAgent({digest("new-phone"): "acct_owner", digest("primary"): "acct_owner"})
        account = self.store(agent).authenticate("Bearer new-phone")
        self.assertEqual((account.id, account.kind), ("acct_owner", "owner"))
        self.assertEqual(agent.asked[0]["body"], {"token_sha256": digest("new-phone")}, "只送雜湊")
        self.assertEqual(agent.asked[0]["token"], "kick")

    def test_revoked_token_is_refused_even_if_the_static_file_knows_it(self):
        agent = FakeAgent({})
        self.assertIsNone(self.store(agent).authenticate("Bearer primary"))

    def test_revoked_primary_stays_refused_when_the_agent_goes_down(self):
        """審核 R1：Agent 說過撤銷，之後 Agent 掉線 ⇒ 靜態表還認得也不能恢復。"""
        agent = FakeAgent({})                      # primary 已撤銷
        store = self.store(agent)
        self.assertIsNone(store.authenticate("Bearer primary"))
        agent.down = True
        self.now[0] += 61                          # 快取過期、改問 Agent ⇒ 問不到
        self.assertIsNone(store.authenticate("Bearer primary"))
        self.now[0] += 3600
        self.assertIsNone(store.authenticate("Bearer primary"), "多久都一樣")

    def test_recently_verified_token_survives_a_short_outage(self):
        agent = FakeAgent({digest("primary"): "acct_owner"})
        store = self.store(agent)
        self.assertEqual(store.authenticate("Bearer primary").id, "acct_owner")
        agent.down = True
        self.now[0] += 300                         # 上次確認 5 分鐘前、Agent 掉線
        self.assertEqual(store.authenticate("Bearer primary").id, "acct_owner")

    def test_revocation_has_an_upper_bound_while_the_agent_is_down(self):
        """撤銷之後 Agent 馬上掉線：上次確認還是「有效」，最多撐到寬限期滿。"""
        agent = FakeAgent({digest("primary"): "acct_owner"})
        store = self.store(agent, grace_s=600.0)
        store.authenticate("Bearer primary")       # t=0 確認有效
        agent.answers.clear()                      # 主人撤銷了……
        agent.down = True                          # ……Agent 隨即掉線
        self.now[0] = 599
        self.assertIsNotNone(store.authenticate("Bearer primary"))
        self.now[0] = 601
        self.assertIsNone(store.authenticate("Bearer primary"), "寬限期滿就拒，不再無上限")

    def test_never_verified_human_token_is_refused_when_the_agent_is_down(self):
        store = self.store(FakeAgent({}, down=True))
        self.assertIsNone(store.authenticate("Bearer primary"), "靜態表認得也不行：從沒確認過")
        self.assertIsNone(store.authenticate("Bearer new-phone"))

    def test_entity_tokens_unaffected_by_a_partition(self):
        store = self.store(FakeAgent({}, down=True))
        self.assertEqual(store.authenticate("Bearer aster-token").id, "aster")

    def test_sse_recheck_uses_the_same_rule(self):
        """http.py 的常駐 SSE 每 30 秒重驗呼叫的就是 `authenticate()`。"""
        source = (pathlib.Path(__file__).resolve().parents[1] / "server" / "http.py").read_text(encoding="utf-8")
        self.assertIn("still = self.services.accounts.authenticate(", source)

    def test_entities_are_not_asked_and_answers_are_cached(self):
        agent = FakeAgent({digest("guest"): "acct_g"})
        store = self.store(agent)
        self.assertEqual(store.authenticate("Bearer aster-token").id, "aster")
        self.assertEqual(agent.asked, [])
        store.authenticate("Bearer guest")
        store.authenticate("Bearer guest")
        self.assertEqual(len(agent.asked), 1, "60 秒內同一把只問一次")
        self.now[0] += 61
        store.authenticate("Bearer guest")
        self.assertEqual(len(agent.asked), 2)

    def test_agent_cannot_hand_out_entity_accounts(self):
        agent = FakeAgent({digest("odd"): "aster"})
        self.assertIsNone(self.store(agent).authenticate("Bearer odd"))

    def test_without_kick_token_nothing_changes(self):
        store = AccountStore(self.path, introspector=Introspector("http://agent:18791", ""))
        self.assertEqual(store.authenticate("Bearer primary").id, "acct_owner")


if __name__ == "__main__":
    unittest.main(verbosity=2)
