#!/usr/bin/env python3
""" Provision App Server accounts without exposing owner or entity tokens.

Run inside a container with Agent data mounted read-only and two independent
writable directories for App Server data and secrets. Existing entity tokens
are reused; missing ones are generated once. Output never contains tokens.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import secrets
import tempfile

_ACCOUNT_ID = re.compile(r"[a-z0-9_-]{1,64}")
_HASH = re.compile(r"[0-9a-f]{64}")
_TOKEN_KEY = "BLUEAGENT_APP_SERVER_TOKENS"


def _read_env(path: pathlib.Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip():
            result[key.strip()] = value
    return result


def _atomic_write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _legacy_rows(path: pathlib.Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(rows, dict):
        raise ValueError("Agent accounts file requires an accounts object")
    return [dict(row) for row in rows.values() if isinstance(row, dict)]


def provision(agent_accounts: pathlib.Path, app_accounts: pathlib.Path,
              secret_file: pathlib.Path, entities: list[str]) -> dict[str, int]:
    rows = _legacy_rows(agent_accounts)
    accounts = []
    migration_accounts = []
    for row in rows:
        identity = str(row.get("account_id") or "")
        role = str(row.get("role") or "")
        digest = str(row.get("token_sha256") or "").lower()
        if role not in {"master", "guest"}:
            continue
        if not _ACCOUNT_ID.fullmatch(identity) or not _HASH.fullmatch(digest):
            continue
        revoked = bool(row.get("revoked_at"))
        if revoked and role != "guest":
            continue
        target = migration_accounts if revoked else accounts
        target.append({
            "id": identity,
            "kind": "owner" if role == "master" else "guest",
            "display": str(row.get("display") or row.get("principal") or identity),
            "legacy_principal": str(row.get("principal") or identity),
            # 抹消済み身份只保留历史归属，旧 token 在 App Server 永远不可认证。
            "token_sha256": "0" * 64 if revoked else digest,
        })
    if sum(row["kind"] == "owner" for row in accounts) != 1:
        raise ValueError("exactly one active Agent master account is required")

    env = _read_env(secret_file)
    try:
        tokens = json.loads(env.get(_TOKEN_KEY) or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("existing App Server token mapping is invalid") from exc
    if not isinstance(tokens, dict):
        raise ValueError("existing App Server token mapping must be an object")
    tokens = {str(key): str(value) for key, value in tokens.items() if str(value)}
    requested = sorted(set(entities))
    if any(not _ACCOUNT_ID.fullmatch(entity) for entity in requested):
        raise ValueError("invalid entity id")
    if any(row["id"] in requested for row in accounts):
        raise ValueError("entity id collides with an existing human account")
    for entity in requested:
        tokens.setdefault(entity, secrets.token_urlsafe(32))
        accounts.append({
            "id": entity,
            "kind": "entity",
            "display": entity,
            "token_sha256": hashlib.sha256(tokens[entity].encode("utf-8")).hexdigest(),
        })

    # Preserve unrelated settings in the dedicated env file, replacing only our key.
    lines = [f"{key}={value}" for key, value in env.items() if key != _TOKEN_KEY]
    lines.append(_TOKEN_KEY + "=" + json.dumps(
        {key: tokens[key] for key in requested}, ensure_ascii=False,
        separators=(",", ":"), sort_keys=True))
    _atomic_write(secret_file, "\n".join(lines) + "\n")
    _atomic_write(app_accounts, json.dumps({
        "accounts": accounts, "migration_accounts": migration_accounts,
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"humans": sum(row["kind"] != "entity" for row in accounts),
            "entities": len(requested)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-accounts", required=True, type=pathlib.Path)
    parser.add_argument("--app-accounts", required=True, type=pathlib.Path)
    parser.add_argument("--secret-file", required=True, type=pathlib.Path)
    parser.add_argument("--entities", default="mira,aster,nori")
    args = parser.parse_args()
    entities = [item.strip() for item in args.entities.split(",") if item.strip()]
    result = provision(args.agent_accounts, args.app_accounts, args.secret_file, entities)
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
