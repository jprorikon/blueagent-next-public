"""legacy data/media 的一次性迁移；默认 dry-run，账号歧义与内容冲突绝不猜测。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib

from app.server.chat.migration import _resolve_account
from app.server.social.models import BoardError


def _rows(index_path: pathlib.Path) -> tuple[list[dict], int]:
    latest = {}
    malformed = 0
    try:
        lines = index_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], 0
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or not row.get("id"):
                raise ValueError("bad media row")
            latest[str(row["id"])] = row
        except (ValueError, TypeError):
            malformed += 1
    return list(latest.values()), malformed


def migrate_media(index_path, media=None, accounts=None, *, apply: bool = False) -> dict:
    index_path = pathlib.Path(index_path)
    rows, malformed = _rows(index_path)
    stats = {"dry_run": not apply, "media": len(rows), "planned": 0, "imported": 0,
             "idempotent_replays": 0, "conflicts": 0, "malformed": malformed,
             "bytes": 0, "errors": []}
    if apply and (media is None or accounts is None):
        raise ValueError("apply requires media and accounts")
    for row in rows:
        media_id = str(row.get("id") or "")
        try:
            principal = str(row.get("principal") or "")
            account = _resolve_account(accounts, principal) if accounts is not None else None
            if accounts is not None and account is None:
                raise ValueError("unknown_or_ambiguous_account")
            source = index_path.parent / (media_id + str(row.get("ext") or "").lower())
            data = source.read_bytes()
            expected = int(row.get("bytes") or len(data))
            if len(data) != expected:
                raise ValueError("size_mismatch")
            stats["planned"] += 1
            stats["bytes"] += len(data)
            if not apply:
                continue
            owner_id = account.id
            digest = hashlib.sha256(data).hexdigest()
            existing = media._row(media_id)
            if existing is not None and existing["sha256"] == digest \
                    and existing["owner_id"] == owner_id:
                stats["idempotent_replays"] += 1
                continue
            media.import_legacy(row, data, owner_id)
            stats["imported"] += 1
        except (OSError, ValueError, TypeError, BoardError) as exc:
            stats["conflicts"] += 1
            code = exc.code if isinstance(exc, BoardError) else str(exc)
            if isinstance(exc, FileNotFoundError):
                code = "missing_file"
            stats["errors"].append({"id": media_id, "error": code[:120]})
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=pathlib.Path, help="legacy data/media/index.jsonl（只读）")
    parser.add_argument("--apply", action="store_true", help="显式写入；省略时只做 dry-run 对账")
    args = parser.parse_args(argv)
    from app.server.config import AppConfig
    from app.server.services import AppServices
    services = AppServices(AppConfig.from_env())
    try:
        result = migrate_media(args.index, services.media, services.accounts, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["conflicts"] else 0
    finally:
        services.close()


if __name__ == "__main__":
    raise SystemExit(main())
