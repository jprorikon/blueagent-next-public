"""旧 Agent 公共区的一次性迁移；默认 dry-run，冲突时绝不写入或回放事件。"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import time

from app.server.chat.migration import _resolve_account
from app.server.social.models import Post
from app.server.social.store import BoardStore

_IMMUTABLE = ("id", "at", "author_kind", "author_id", "origin", "source_ref",
              "body", "media", "kind", "visibility", "parent_id")


def _signature(post: Post) -> tuple:
    row = post.to_row()
    return tuple(json.dumps(row[key], ensure_ascii=False, sort_keys=True)
                 if isinstance(row[key], (list, dict)) else row[key] for key in _IMMUTABLE)


def _jsonl_rows(root: pathlib.Path) -> tuple[list[Post], list[dict]]:
    posts = []
    errors = []
    for path in sorted(root.glob("*.jsonl")) if root.is_dir() else []:
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="strict").splitlines(), 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or not row.get("id"):
                    raise ValueError("bad_post_row")
                posts.append(Post.from_row(row))
            except (ValueError, TypeError, UnicodeError) as exc:
                errors.append({"id": f"{path.name}:{number}", "error": str(exc)[:120]})
    return posts, errors


def _read_source(root: pathlib.Path) -> tuple[dict, list[Post], list[dict]]:
    result = {"posts": [], "reactions": [], "counters": [], "mutations": []}
    path = root / "board.db"
    if not path.is_file():
        return result, *_jsonl_rows(root)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        result["posts"] = [dict(row) for row in db.execute("SELECT * FROM post")]
        result["reactions"] = [dict(row) for row in db.execute("SELECT * FROM reaction")]
        result["counters"] = [dict(row) for row in db.execute("SELECT * FROM counter")]
        result["mutations"] = [dict(row) for row in db.execute("SELECT * FROM mutation")]
    finally:
        db.close()
    jsonl, errors = _jsonl_rows(root)
    result["jsonl"] = [post.to_row() for post in jsonl]
    return result, jsonl, errors


def _merge_post(posts: dict[str, Post], post: Post, errors: list[dict]) -> None:
    if post.origin != "manual":
        return
    old = posts.get(post.id)
    if old is not None and _signature(old) != _signature(post):
        errors.append({"id": post.id, "error": "source_post_conflict"})
        return
    if old is None:
        posts[post.id] = post
    elif post.deleted_at and not old.deleted_at:
        old.deleted_at, old.deleted_by = post.deleted_at, post.deleted_by


def _map_response(value, accounts, errors: list[dict]):
    if isinstance(value, list):
        return [_map_response(item, accounts, errors) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    mapped_display = None
    if value.get("author_id"):
        account = _resolve_account(accounts, str(value["author_id"]))
        if account is None:
            errors.append({"id": str(value.get("id") or "mutation_response"),
                           "error": "unknown_or_ambiguous_account"})
        else:
            mapped_display = account.display
    for key, item in value.items():
        if key == "author_id":
            account = _resolve_account(accounts, str(item))
            result[key] = account.id if account is not None else str(item)
        elif key == "display" and mapped_display is not None:
            result[key] = mapped_display
        else:
            result[key] = _map_response(item, accounts, errors)
    return result


def _load_projection(path) -> list[Post]:
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    rows = payload.get("posts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("projection snapshot requires a posts array")
    posts = [Post.from_row(row) for row in rows if isinstance(row, dict)]
    if len(posts) != len(rows) or any(post.origin != "projection" or not post.id for post in posts):
        raise ValueError("invalid projection snapshot")
    return posts


def _destination_jsonl(root: pathlib.Path) -> tuple[dict[str, list[Post]], list[dict]]:
    rows, errors = _jsonl_rows(root)
    result = {}
    for post in rows:
        result.setdefault(post.id, []).append(post)
    return result, errors


def _prepare_migration(legacy_data, projections, store: BoardStore, accounts) -> dict:
    """只读收集、账号映射与目标冲突检查；任何错误都不得进入写入阶段。"""
    legacy_data = pathlib.Path(legacy_data)
    source_root = legacy_data / "board"
    source, jsonl_posts, errors = _read_source(source_root)
    posts: dict[str, Post] = {}
    projection_tombstones = {}
    for row in source["posts"]:
        post = Post.from_row(row)
        if post.origin == "projection":
            projection_tombstones[post.id] = post
        else:
            _merge_post(posts, post, errors)
    for post in jsonl_posts:
        _merge_post(posts, post, errors)
    for projected in projections:
        if projected.id in posts and _signature(posts[projected.id]) != _signature(projected):
            errors.append({"id": projected.id, "error": "projection_collides_with_manual"})
            continue
        old = projection_tombstones.get(projected.id)
        if old is not None and old.deleted_at:
            projected.deleted_at, projected.deleted_by = old.deleted_at, old.deleted_by
        posts[projected.id] = projected

    mapped_posts: dict[str, Post] = {}
    for post_id, source_post in sorted(posts.items()):
        account = _resolve_account(accounts, source_post.author_id)
        if account is None:
            errors.append({"id": post_id, "error": "unknown_or_ambiguous_account"})
            continue
        deleted_by = ""
        if source_post.deleted_by:
            deleted_account = _resolve_account(accounts, source_post.deleted_by)
            if deleted_account is None:
                errors.append({"id": post_id, "error": "unknown_deleted_by"})
                continue
            deleted_by = deleted_account.id
        mapped = Post.from_row(source_post.to_row())
        mapped.author_id, mapped.author_kind, mapped.display = account.id, account.kind, account.display
        mapped.deleted_by = deleted_by
        mapped_posts[post_id] = mapped

    source_refs = {}
    for post in mapped_posts.values():
        if post.source_ref:
            previous = source_refs.setdefault(post.source_ref, post.id)
            if previous != post.id:
                errors.append({"id": post.id, "error": "source_ref_reused"})
        if post.parent_id:
            parent = mapped_posts.get(post.parent_id)
            if parent is None or parent.parent_id:
                errors.append({"id": post.id, "error": "missing_or_nested_parent"})
        if post.visibility != "public":
            errors.append({"id": post.id, "error": "non_public_migration_refused"})
        if post.origin == "projection" and not post.source_ref:
            errors.append({"id": post.id, "error": "projection_source_ref_required"})

    reactions = []
    for row in source["reactions"]:
        post_id = str(row.get("post_id") or "")
        account = _resolve_account(accounts, str(row.get("actor_id") or ""))
        if post_id not in mapped_posts or account is None:
            errors.append({"id": post_id, "error": "reaction_mapping_conflict"})
            continue
        reactions.append({"post_id": post_id, "actor_id": account.id,
                          "kind": str(row.get("kind") or ""), "at": float(row.get("at") or 0)})

    counters = []
    for row in source["counters"]:
        post_id = str(row.get("post_id") or "")
        if post_id not in mapped_posts:
            errors.append({"id": post_id, "error": "counter_mapping_conflict"})
            continue
        counters.append({"post_id": post_id, "likes": max(0, int(row.get("likes") or 0)),
                         "replies": max(0, int(row.get("replies") or 0))})

    mutations = []
    for row in source["mutations"]:
        account = _resolve_account(accounts, str(row.get("actor_id") or ""))
        mutation_id = str(row.get("client_mutation_id") or "")
        try:
            request = json.loads(row["request_json"])
            response = _map_response(json.loads(row["response_json"]), accounts, errors)
            if account is None or not isinstance(request, dict) or not isinstance(response, dict):
                raise ValueError("mutation_mapping_conflict")
            mutations.append({"actor_id": account.id, "client_mutation_id": mutation_id,
                              "kind": str(row["kind"]), "request": request, "response": response,
                              "created_at": float(row["created_at"])})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            errors.append({"id": mutation_id, "error": str(exc)[:120]})

    destination_jsonl, destination_errors = _destination_jsonl(store.root)
    errors.extend(destination_errors)
    for post_id, post in mapped_posts.items():
        existing = store.get(post_id)
        if existing is not None and _signature(existing) != _signature(post):
            errors.append({"id": post_id, "error": "destination_post_conflict"})
        for original in destination_jsonl.get(post_id, []):
            if post.origin == "manual" and _signature(original) != _signature(post):
                errors.append({"id": post_id, "error": "destination_jsonl_conflict"})
        if post.source_ref:
            source_owner = store.by_source_ref(post.source_ref)
            if source_owner is not None and source_owner.id != post_id:
                errors.append({"id": post_id, "error": "destination_source_ref_conflict"})

    for row in reactions:
        with store._lock:
            old = store._db.execute(
                "SELECT at FROM reaction WHERE post_id=? AND actor_id=? AND kind=?",
                (row["post_id"], row["actor_id"], row["kind"])).fetchone()
        if old is not None and float(old["at"]) != row["at"]:
            errors.append({"id": row["post_id"], "error": "destination_reaction_conflict"})
    for row in mutations:
        with store._lock:
            old = store._db.execute(
                "SELECT * FROM mutation WHERE actor_id=? AND client_mutation_id=?",
                (row["actor_id"], row["client_mutation_id"])).fetchone()
        if old is not None and (old["kind"] != row["kind"]
                                or old["request_json"] != store._json(row["request"])
                                or old["response_json"] != store._json(row["response"])):
            errors.append({"id": row["client_mutation_id"], "error": "destination_mutation_conflict"})

    # 同一错误只报告一次，便于 dry-run 人工对账。
    unique_errors = list({(row["id"], row["error"]): row for row in errors}.values())
    stats = {
        "dry_run": True, "posts": len(mapped_posts),
        "manual_posts": sum(post.origin == "manual" and not post.parent_id
                            for post in mapped_posts.values()),
        "replies": sum(bool(post.parent_id) for post in mapped_posts.values()),
        "projections": sum(post.origin == "projection" for post in mapped_posts.values()),
        "soft_deleted": sum(bool(post.deleted_at) for post in mapped_posts.values()),
        "reactions": len(reactions), "counters": len(counters), "mutations": len(mutations),
        "imported_posts": 0, "imported_reactions": 0, "imported_counters": 0,
        "imported_mutations": 0, "restored_deletions": 0, "idempotent_replays": 0,
        "conflicts": len(unique_errors), "errors": unique_errors,
        "reconciliation": {}, "ok": not unique_errors,
    }
    return {"stats": stats, "posts": mapped_posts, "reactions": reactions,
            "counters": counters, "mutations": mutations, "destination_jsonl": destination_jsonl}


def _apply_migration(store: BoardStore, plan: dict) -> dict:
    """先写原文，再在一个无事件事务中写索引；最后独立重读对账。"""
    stats = plan["stats"]
    stats["dry_run"] = False
    mapped_posts = plan["posts"]
    reactions, counters, mutations = plan["reactions"], plan["counters"], plan["mutations"]
    destination_jsonl = plan["destination_jsonl"]
    # 原文先追加；DB 事务失败时下一次 replay 会按同一 ID 对账并修复索引。
    pending_jsonl = [post for post in mapped_posts.values() if post.origin == "manual"
                     and not any(_signature(old) == _signature(post)
                                 for old in destination_jsonl.get(post.id, []))]
    by_month: dict[str, list[Post]] = {}
    for post in pending_jsonl:
        by_month.setdefault(time.strftime("%Y-%m.jsonl", time.localtime(post.at)), []).append(post)
    for name, rows in sorted(by_month.items()):
        with (store.root / name).open("a", encoding="utf-8") as stream:
            for post in sorted(rows, key=lambda item: (item.at, item.id)):
                stream.write(json.dumps(post.to_row(), ensure_ascii=False) + "\n")
            stream.flush()

    with store._lock, store._db:
        for post_id, post in sorted(mapped_posts.items()):
            old = store._db.execute("SELECT deleted_at FROM post WHERE id=?", (post_id,)).fetchone()
            if old is None:
                store._db.execute("INSERT INTO post VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  store._row(post))
                stats["imported_posts"] += 1
            else:
                stats["idempotent_replays"] += 1
                if post.deleted_at and not old["deleted_at"]:
                    store._db.execute("UPDATE post SET deleted_at=?,deleted_by=? WHERE id=?",
                                      (post.deleted_at, post.deleted_by, post_id))
                    stats["restored_deletions"] += 1
        for row in reactions:
            cursor = store._db.execute(
                "INSERT OR IGNORE INTO reaction(post_id,actor_id,kind,at) VALUES (?,?,?,?)",
                (row["post_id"], row["actor_id"], row["kind"], row["at"]))
            stats["imported_reactions" if cursor.rowcount else "idempotent_replays"] += int(bool(cursor.rowcount))
            stats["idempotent_replays"] += int(not cursor.rowcount)
        for row in counters:
            old = store._db.execute("SELECT likes,replies FROM counter WHERE post_id=?",
                                    (row["post_id"],)).fetchone()
            if old is None:
                store._db.execute("INSERT INTO counter(post_id,likes,replies) VALUES (?,?,?)",
                                  (row["post_id"], row["likes"], row["replies"]))
                stats["imported_counters"] += 1
            else:
                stats["idempotent_replays"] += 1
                # 平台切换后的新互动只会增加；重放历史绝不把它降回去。
                store._db.execute("UPDATE counter SET likes=MAX(likes,?),replies=MAX(replies,?) "
                                  "WHERE post_id=?",
                                  (row["likes"], row["replies"], row["post_id"]))
        for row in mutations:
            cursor = store._db.execute("""
              INSERT OR IGNORE INTO mutation(actor_id,client_mutation_id,kind,request_json,
                                             response_json,created_at)
              VALUES (?,?,?,?,?,?)
            """, (row["actor_id"], row["client_mutation_id"], row["kind"],
                  store._json(row["request"]), store._json(row["response"]), row["created_at"]))
            if cursor.rowcount:
                stats["imported_mutations"] += 1
            else:
                stats["idempotent_replays"] += 1

    reconciliation = {"different_posts": [], "missing_reactions": [],
                      "counter_regressions": [], "different_mutations": [], "jsonl_conflicts": []}
    final_jsonl, jsonl_errors = _destination_jsonl(store.root)
    reconciliation["jsonl_conflicts"].extend(row["id"] for row in jsonl_errors)
    for post_id, post in sorted(mapped_posts.items()):
        actual = store.get(post_id)
        if actual is None or _signature(actual) != _signature(post) \
                or (post.deleted_at and not actual.deleted_at):
            reconciliation["different_posts"].append(post_id)
        if post.origin == "manual" and not any(
                _signature(old) == _signature(post) for old in final_jsonl.get(post_id, [])):
            reconciliation["jsonl_conflicts"].append(post_id)
    with store._lock:
        for row in reactions:
            found = store._db.execute(
                "SELECT 1 FROM reaction WHERE post_id=? AND actor_id=? AND kind=? AND at=?",
                (row["post_id"], row["actor_id"], row["kind"], row["at"])).fetchone()
            if found is None:
                reconciliation["missing_reactions"].append(row["post_id"])
        for row in counters:
            actual = store._db.execute("SELECT likes,replies FROM counter WHERE post_id=?",
                                       (row["post_id"],)).fetchone()
            if actual is None or int(actual["likes"]) < row["likes"] \
                    or int(actual["replies"]) < row["replies"]:
                reconciliation["counter_regressions"].append(row["post_id"])
        for row in mutations:
            actual = store._db.execute(
                "SELECT * FROM mutation WHERE actor_id=? AND client_mutation_id=?",
                (row["actor_id"], row["client_mutation_id"])).fetchone()
            if actual is None or actual["kind"] != row["kind"] \
                    or actual["request_json"] != store._json(row["request"]) \
                    or actual["response_json"] != store._json(row["response"]):
                reconciliation["different_mutations"].append(row["client_mutation_id"])
    stats["reconciliation"] = reconciliation
    stats["ok"] = not any(reconciliation.values())
    return stats


def migrate_board(legacy_data, projections, store: BoardStore, accounts, *, apply: bool = False) -> dict:
    """保留 ID／正文／互动／软删／幂等证据；目标新增状态绝不被旧快照回滚。"""
    plan = _prepare_migration(legacy_data, projections, store, accounts)
    if not apply or not plan["stats"]["ok"]:
        return plan["stats"]
    return _apply_migration(store, plan)


def _backup_db(source: pathlib.Path, target: pathlib.Path) -> None:
    if not source.is_file():
        return
    db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(target)
        try:
            db.backup(destination)
        finally:
            destination.close()
    finally:
        db.close()


def backup_boards(legacy_data, destination_root, backup_dir) -> dict:
    """SQLite 在线一致性备份＋双方月档；目录必须全新，避免覆盖旧恢复点。"""
    backup = pathlib.Path(backup_dir)
    if backup.exists():
        raise ValueError("backup directory must not already exist")
    legacy_board = pathlib.Path(legacy_data) / "board"
    destination_board = pathlib.Path(destination_root) / "board"
    for name, source in (("legacy", legacy_board), ("destination", destination_board)):
        target = backup / name
        target.mkdir(parents=True)
        _backup_db(source / "board.db", target / "board.db")
        if source.is_dir():
            for path in source.glob("*.jsonl"):
                shutil.copy2(path, target / path.name)
    return {"backup_dir": str(backup)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("legacy_data", type=pathlib.Path, help="旧 Agent data（只读）")
    parser.add_argument("--projection-json", required=True, type=pathlib.Path,
                        help="由 Agent 当前 public activity／works 生成的一次性快照")
    parser.add_argument("--backup-dir", type=pathlib.Path, help="apply 必填的全新备份目录")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    from app.server.config import AppConfig
    from app.server.services import AppServices
    services = AppServices(AppConfig.from_env())
    try:
        if args.apply:
            print(json.dumps(backup_boards(args.legacy_data, services.config.data_root,
                                           args.backup_dir), ensure_ascii=False))
        result = migrate_board(args.legacy_data, _load_projection(args.projection_json),
                               services.board.store, services.accounts, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    finally:
        services.close()


if __name__ == "__main__":
    raise SystemExit(main())
