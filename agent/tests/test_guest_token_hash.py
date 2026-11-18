"""guests.json 不再長期存明文 token（2026-10-31）：只留 sha256，明文只在「批准了、guest 還沒來拿」那段時間存在。"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.guest import RelationDeclaration, hash_guest_token
from agent.brain.runtime.identity.guest_store import GuestStore


def _approved(store: GuestStore):
    code = store.create_invite(uses=1)["code"]
    request = store.create_request(invite_code=code, discord_id="discord-1", display_name="Alice",
                                   declared=RelationDeclaration(kind="friend", label="友達"))
    return store.approve(request.id, final=request.declared, trust=0.5, capabilities=("chat",))


class TestGuestTokenHash(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp(prefix="ba-guest-hash-")) / "guests.json"

    def _disk(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))["requests"]

    def test_plaintext_lives_only_until_delivered(self):
        store = GuestStore(self.path)
        request = _approved(store)
        token = request.token
        self.assertTrue(token.startswith("gt_"))
        self.assertEqual(self._disk()[0]["token_sha256"], hash_guest_token(token))

        store.deliver_token(request)                         # guest 用 request_secret 拿走了
        row = self._disk()[0]
        self.assertEqual(row["token"], "")
        self.assertNotIn(token, self.path.read_text(encoding="utf-8"))
        self.assertIs(store.by_token(token), request)        # 之後照樣認得（比雜湊）
        self.assertIsNone(store.by_token("gt_wrong"))
        self.assertIsNone(store.by_token(""))

    def test_legacy_plaintext_file_is_converted_on_load(self):
        store = GuestStore(self.path)
        request = _approved(store)
        token = request.token
        rows = self._disk()
        rows[0].pop("token_sha256")                          # 舊格式：只有明文
        self.path.write_text(json.dumps({"requests": rows, "invites": {}}), encoding="utf-8")

        reloaded = GuestStore(self.path)
        self.assertIsNotNone(reloaded.by_token(token))
        loaded = reloaded.accounts()[0]
        self.assertEqual(loaded.token, "")
        self.assertEqual(loaded.token_sha256, hash_guest_token(token))

    def test_status_hands_out_token_once(self):
        state = make_state()
        try:
            store = state.app.guest_store
            code = store.create_invite(uses=1)["code"]
            joined = state.join_guest(invite_code=code, discord_id="d-2", display_name="Bob",
                                      declared_kind="friend", declared_label="友達", declared_note="",
                                      guest_relations=())
            request = store.by_secret(joined["request_secret"])
            store.approve(request.id, final=request.declared, trust=0.5, capabilities=("chat",))
            first = state.guest_status(joined["request_secret"])["request"]
            second = state.guest_status(joined["request_secret"])["request"]
            self.assertTrue(first["token"].startswith("gt_"))
            self.assertEqual(second["token"], "")
            self.assertNotIn("token_sha256", first)
            self.assertIsNotNone(state.app.auth.authenticate("Bearer " + first["token"]))
        finally:
            state.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
