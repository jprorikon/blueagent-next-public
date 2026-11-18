#!/usr/bin/env python3
"""C-02：旧 JSONL／SQLite 全量迁移、账号映射、幂等重放与冲突拒写。"""

from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile
import time
import unittest

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.accounts import AccountStore
from app.server.events import EventLog
from app.server.social.migration import backup_boards, migrate_board
from app.server.social.models import Actor, Post
from app.server.social.service import BoardService


class BoardMigrationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-board-migration-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.legacy_root = self.root / "legacy"
        (self.legacy_root / "logs").mkdir(parents=True)
        self.legacy = BoardService(self.legacy_root, clock=lambda: 1000.0)
        self.addCleanup(self.legacy.close)
        self.owner = Actor("owner", "owner", "旧主人")
        self.guest = Actor("guest-old", "guest", "旧客人")
        self.accounts_path = self.root / "accounts.json"
        rows = [
            ("acct-owner", "owner", "主人", "owner"),
            ("acct-guest", "guest", "客人", "guest-old"),
            ("acct-other", "guest", "别人", "guest-other"),
            ("aster", "entity", "アスター", ""),
        ]
        self.accounts_path.write_text(json.dumps({"accounts": [
            {"id": identity, "kind": kind, "display": display, "legacy_principal": legacy,
             "token_sha256": hashlib.sha256((identity + "-token").encode()).hexdigest()}
            for identity, kind, display, legacy in rows]}), encoding="utf-8")
        self.accounts = AccountStore(self.accounts_path)
        self.target = BoardService(self.root / "target")
        self.addCleanup(self.target.close)
        self.projection = Post(id="pst_act_aster", at=900.0, author_kind="entity",
                               author_id="aster", display="アスター", body="角色公开快照",
                               origin="projection", kind="mutter",
                               source_ref="activity:aster:final")

    def fixture(self):
        root = self.legacy.post(self.owner, "保留根帖", client_mutation_id="cm-old")["post"]
        reply = self.legacy.reply(self.guest, root["id"], "保留回复")["post"]
        self.legacy.like(self.guest, root["id"])
        deleted = self.legacy.post(self.owner, "保留软删")["post"]
        self.legacy.delete(self.owner, deleted["id"])
        self.legacy.store.upsert_projection(self.projection)
        self.legacy.store.like(self.projection.id, self.guest.id, 1000.0, self.guest.kind)
        orphan = Post(id="pst_jsonl_only", at=1001.0, author_kind="guest",
                      author_id="guest-old", display="旧客人", body="只在月档里的原文")
        path = self.legacy_root / "board" / time.strftime("%Y-%m.jsonl", time.localtime(orphan.at))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(orphan.to_row(), ensure_ascii=False) + "\n")
        return root, reply, deleted, orphan

    def test_dry_apply_replay_preserves_all_truth_without_events(self):
        root, reply, deleted, orphan = self.fixture()
        before = self.target.store.canonical()
        dry = migrate_board(self.legacy_root, [self.projection], self.target.store,
                            self.accounts)
        self.assertTrue(dry["ok"], dry)
        self.assertEqual((dry["posts"], dry["manual_posts"], dry["replies"],
                          dry["projections"], dry["soft_deleted"]), (5, 3, 1, 1, 1), dry)
        self.assertEqual(self.target.store.canonical(), before)

        events = EventLog(self.target.store.db_path)
        self.addCleanup(events.close)
        first = migrate_board(self.legacy_root, [self.projection], self.target.store,
                              self.accounts, apply=True)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["imported_posts"], 5, first)
        self.assertEqual(first["imported_reactions"], 2, first)
        self.assertEqual(first["imported_mutations"], 1, first)
        self.assertEqual(events.list()["events"], [], "历史迁移不得回放社交事件")
        self.assertEqual(self.target.store.get(root["id"]).author_id, "acct-owner")
        self.assertEqual(self.target.store.get(reply["id"]).author_id, "acct-guest")
        self.assertEqual(self.target.store.get(orphan.id).body, "只在月档里的原文")
        self.assertTrue(self.target.store.get(deleted["id"]).deleted_at)
        self.assertEqual(self.target.store.get(self.projection.id).body, "角色公开快照")
        self.assertEqual(self.target.store.counters([root["id"]], "acct-guest")[root["id"]],
                         {"likes": 1, "replies": 1, "liked": True})
        cached = self.target.store.mutation("acct-owner", "cm-old", "post.create",
                                            {"text": "保留根帖", "media": []})
        self.assertEqual(cached["post"]["author_id"], "acct-owner")

        original_files = {path.name: path.read_bytes()
                          for path in self.target.store.root.glob("*.jsonl")}
        second = migrate_board(self.legacy_root, [self.projection], self.target.store,
                               self.accounts, apply=True)
        self.assertTrue(second["ok"], second)
        self.assertEqual((second["imported_posts"], second["imported_reactions"],
                          second["imported_mutations"]), (0, 0, 0), second)
        self.assertGreater(second["idempotent_replays"], 0)
        self.assertEqual(original_files, {path.name: path.read_bytes()
                                          for path in self.target.store.root.glob("*.jsonl")})

        other = Actor("acct-other", "guest", "别人")
        self.target.like(other, root["id"])
        third = migrate_board(self.legacy_root, [self.projection], self.target.store,
                              self.accounts, apply=True)
        self.assertTrue(third["ok"], third)
        self.assertEqual(self.target.store.counters([root["id"]], "acct-other")[root["id"]]["likes"],
                         2, "重放不得降低平台上的新互动")

    def test_unknown_account_and_destination_conflict_fail_without_writes(self):
        root, _, _, _ = self.fixture()
        unknown = Post(id="pst_unknown", at=1002.0, author_kind="guest",
                       author_id="missing", display="不存在", body="不能猜作者")
        path = next((self.legacy_root / "board").glob("*.jsonl"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(unknown.to_row(), ensure_ascii=False) + "\n")
        dry = migrate_board(self.legacy_root, [self.projection], self.target.store,
                            self.accounts)
        self.assertFalse(dry["ok"])
        self.assertGreater(dry["conflicts"], 0)
        blocked = migrate_board(self.legacy_root, [self.projection], self.target.store,
                                self.accounts, apply=True)
        self.assertEqual(blocked["imported_posts"], 0)
        self.assertEqual(self.target.store.canonical(), [])

        self.target.store.create_manual(Post(id=root["id"], at=1000.0, author_kind="owner",
                                             author_id="acct-owner", display="主人", body="冲突正文"))
        conflict = migrate_board(self.legacy_root, [self.projection], self.target.store,
                                 self.accounts, apply=True)
        self.assertFalse(conflict["ok"])
        self.assertEqual(self.target.store.get(root["id"]).body, "冲突正文")

    def test_duplicate_projection_source_ref_fails_before_writing(self):
        other = Post.from_row(self.projection.to_row())
        other.id = "pst_act_aster_other"
        result = migrate_board(self.legacy_root, [self.projection, other],
                               self.target.store, self.accounts, apply=True)
        self.assertFalse(result["ok"])
        self.assertIn("source_ref_reused", {row["error"] for row in result["errors"]})
        self.assertEqual(result["imported_posts"], 0)
        self.assertEqual(self.target.store.canonical(), [])

    def test_backup_contains_consistent_databases_and_jsonl(self):
        self.fixture()
        self.target.post(Actor("acct-owner", "owner", "主人"), "目标已有帖子")
        backup = self.root / "backup"
        backup_boards(self.legacy_root, self.target.store.data_root, backup)
        self.assertTrue((backup / "legacy" / "board.db").is_file())
        self.assertTrue((backup / "destination" / "board.db").is_file())
        self.assertTrue(list((backup / "legacy").glob("*.jsonl")))
        with self.assertRaises(ValueError):
            backup_boards(self.legacy_root, self.target.store.data_root, backup)


if __name__ == "__main__":
    unittest.main(verbosity=2)
