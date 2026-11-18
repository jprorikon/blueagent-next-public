from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest


def _load():
    path = pathlib.Path(__file__).resolve().parents[2] / "agent" / "tools" / "provision_app_server.py"
    spec = importlib.util.spec_from_file_location("provision_app_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AppServerProvisionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ba-app-provision-")
        self.root = pathlib.Path(self.temp.name)
        self.agent = self.root / "agent-accounts.json"
        self.app = self.root / "app" / "accounts.json"
        self.secret = self.root / "secrets" / "agent.env"
        self.agent.write_text(json.dumps({"accounts": {
            "owner": {"account_id": "owner", "role": "master", "display": "Owner",
                      "token_sha256": hashlib.sha256(b"owner-token").hexdigest()},
            "guest-old": {"account_id": "guest-old", "role": "guest",
                          "token_sha256": hashlib.sha256(b"guest-token").hexdigest(),
                          "revoked_at": 1},
        }}), encoding="utf-8")
        self.module = _load()

    def tearDown(self):
        self.temp.cleanup()

    def test_reuses_owner_hash_and_generates_entity_tokens_once(self):
        first = self.module.provision(self.agent, self.app, self.secret, ["aster", "mira"])
        self.assertEqual({"humans": 1, "entities": 2}, first)
        accounts = json.loads(self.app.read_text(encoding="utf-8"))["accounts"]
        self.assertEqual(["aster", "mira", "owner"], sorted(row["id"] for row in accounts))
        owner = next(row for row in accounts if row["id"] == "owner")
        self.assertEqual(hashlib.sha256(b"owner-token").hexdigest(), owner["token_sha256"])
        env_before = self.secret.read_text(encoding="utf-8")
        self.assertNotIn("owner-token", env_before)
        self.assertEqual(0o600, self.secret.stat().st_mode & 0o777)

        second = self.module.provision(self.agent, self.app, self.secret, ["aster", "mira"])
        self.assertEqual(first, second)
        self.assertEqual(env_before, self.secret.read_text(encoding="utf-8"))

    def test_preserves_legacy_principal_for_history_migration(self):
        payload = json.loads(self.agent.read_text(encoding="utf-8"))
        payload["accounts"]["acct_g_1"] = {
            "account_id": "acct_g_1", "role": "guest", "principal": "active-guest",
            "display": "輝石", "token_sha256": hashlib.sha256(b"guest-token").hexdigest(),
        }
        payload["accounts"]["acct_g_revoked"] = {
            "account_id": "acct_g_revoked", "role": "guest", "principal": "guest-707",
            "display": "Alice", "token_sha256": hashlib.sha256(b"revoked-token").hexdigest(),
            "revoked_at": 1,
        }
        self.agent.write_text(json.dumps(payload), encoding="utf-8")
        self.module.provision(self.agent, self.app, self.secret, ["aster"])
        body = json.loads(self.app.read_text(encoding="utf-8"))
        accounts = body["accounts"]
        owner = next(row for row in accounts if row["id"] == "owner")
        guest = next(row for row in accounts if row["id"] == "acct_g_1")
        revoked = next(row for row in body["migration_accounts"]
                       if row["id"] == "acct_g_revoked")
        self.assertEqual("owner", owner["legacy_principal"])
        self.assertEqual("active-guest", guest["legacy_principal"])
        self.assertEqual("guest-707", revoked["legacy_principal"])
        self.assertEqual("0" * 64, revoked["token_sha256"])
        self.assertFalse(any(row["id"] == "acct_g_revoked" for row in accounts))

    def test_requires_exactly_one_active_master(self):
        payload = json.loads(self.agent.read_text(encoding="utf-8"))
        payload["accounts"].clear()
        self.agent.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.module.provision(self.agent, self.app, self.secret, ["aster"])
        self.assertFalse(self.app.exists())
        self.assertFalse(self.secret.exists())


if __name__ == "__main__":
    unittest.main()
