#!/usr/bin/env python3
"""一次性遷移：把 11-07 之後**以 App 帳號 id 存下的身分**改回腦端 principal（owner 2026-11-09）。

背景：App Server 拆出來之後，App 私聊的 principal 一直是帳號 id（`acct_owner`，經 `norm()` 也可能是
`acct-owner`），而不是腦端的 `owner`／`guest-…`。入口現在已經翻譯（`brain/app/identity.py`），
這支把舊資料對齊：

  會改（`--apply`，先備份 `<檔>.bak-identity-<時間>`）：
    · `memory/facts.db`：事實的 `principal` 與 `audience`（房間名冊）——不改的話，App 那邊記下的事
      在主人身上**披露規則對不上**（房間裡是 `acct_owner`、在場的是 `owner`）。
    · `threads.json`：線的 `participants`、每輪的 `speaker`／`addressed_to`。
    · `proactive.json`：`<角色>|acct_owner` 這類鍵。
  只報告、不改（會跟 `owner` 既有的資料撞，要人看過再合）：
    · 線的 `key.scope`（`acct-owner` 的舊線會自然過窗；新的 App 對話進 `owner` 的線）；
    · `users/<帳號 id>.md`、`stances/<角色>.json` 裡的帳號 id 槽、`raw/` 原文（原文只追加，不改）。

用法（容器裡，先停服務或確定沒有寫入）：
    python3 agent/tools/migrate_app_identities.py --data /app/data            # 只看（預設）
    python3 agent/tools/migrate_app_identities.py --data /app/data --apply    # 備份後寫入
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sqlite3
import sys
import time


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def mapping(data: pathlib.Path) -> dict[str, str]:
    """帳號 id（含 `norm()` 後的寫法）→ 腦端 principal；兩者相同的不收。"""
    try:
        raw = json.loads((data / "accounts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = raw.get("accounts") if isinstance(raw, dict) else None
    rows = rows.values() if isinstance(rows, dict) else (rows or [])
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        account = str(row.get("account_id") or "")
        principal = "owner" if str(row.get("role") or "") == "master" else str(row.get("principal") or "")
        for variant in {account, _norm(account)}:
            if variant and principal and variant != principal:
                out[variant] = principal
    return out


def _swap(value: str, table: dict[str, str]) -> str:
    return table.get(str(value or ""), str(value or ""))


def _backup(path: pathlib.Path, stamp: str) -> None:
    shutil.copy2(path, path.with_name(f"{path.name}.bak-identity-{stamp}"))


def facts(data: pathlib.Path, table: dict[str, str], *, apply: bool, stamp: str) -> dict:
    path = data / "memory" / "facts.db"
    if not path.exists():
        return {"file": str(path), "missing": True}
    db = sqlite3.connect(str(path))
    rows = db.execute("SELECT rowid, principal, audience FROM facts").fetchall()
    changes = []
    for rowid, principal, audience in rows:
        members = [item for item in str(audience or "").split(",") if item] if audience is not None else None
        new_members = None if members is None else sorted(dict.fromkeys(_swap(m, table) for m in members))
        new_principal = _swap(principal, table)
        if new_principal != (principal or "") or (members is not None and new_members != sorted(members)):
            changes.append((new_principal, None if new_members is None else ",".join(new_members), rowid))
    if apply and changes:
        db.close()
        _backup(path, stamp)
        db = sqlite3.connect(str(path))
        db.executemany("UPDATE facts SET principal=?, audience=? WHERE rowid=?", changes)
        db.commit()
    db.close()
    return {"file": str(path), "facts": len(rows), "changed": len(changes)}


def threads(data: pathlib.Path, table: dict[str, str], *, apply: bool, stamp: str) -> dict:
    path = data / "threads.json"
    if not path.exists():
        return {"file": str(path), "missing": True}
    body = json.loads(path.read_text(encoding="utf-8"))
    turns_changed = threads_changed = scopes = 0
    for thread in body.get("threads") or []:
        before = list(thread.get("participants") or [])
        after = sorted(dict.fromkeys(_swap(item, table) for item in before))
        touched = after != sorted(before)
        thread["participants"] = after
        for bucket in ("transcript", "segment"):
            for turn in thread.get(bucket) or []:
                for field in ("speaker", "addressed_to"):
                    new = _swap(turn.get(field), table)
                    if new != str(turn.get(field) or ""):
                        turn[field] = new
                        turns_changed += 1
                        touched = True
        if str((thread.get("key") or {}).get("scope") or "") in table:
            scopes += 1
        threads_changed += int(touched)
    if apply and (threads_changed or turns_changed):
        _backup(path, stamp)
        path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"file": str(path), "threads_changed": threads_changed, "turns_changed": turns_changed,
            "scope_left_as_is": scopes}


def proactive(data: pathlib.Path, table: dict[str, str], *, apply: bool, stamp: str) -> dict:
    path = data / "proactive.json"
    if not path.exists():
        return {"file": str(path), "missing": True}
    body = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    for section in ("heard", "sent", "delivered", "read"):
        rows = body.get(section)
        if not isinstance(rows, dict):
            continue
        fresh = {}
        for key, value in rows.items():
            parts = str(key).split("|")
            new_key = "|".join([parts[0]] + [_swap(item, table) for item in parts[1:]])
            changed += int(new_key != key)
            if new_key in fresh and isinstance(value, (int, float)):
                value = max(float(fresh[new_key]), float(value))      # heard：取較晚的那一刻
            elif new_key in fresh and isinstance(value, list):
                value = list(fresh[new_key]) + list(value)
            fresh[new_key] = value
        body[section] = fresh
    if apply and changed:
        _backup(path, stamp)
        path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return {"file": str(path), "keys_changed": changed}


def report_only(data: pathlib.Path, table: dict[str, str]) -> dict:
    users = [str(path) for name in table for path in [data / "users" / f"{name}.md"] if path.exists()]
    stances = []
    for path in sorted((data / "stances").glob("*.json")):
        try:
            rows = (json.loads(path.read_text(encoding="utf-8")).get("principals") or {})
        except (OSError, ValueError):
            continue
        stances += [f"{path.name}:{key}" for key in rows if key in table]
    raw_hits = 0
    for path in sorted((data / "raw").glob("*/*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        raw_hits += sum(text.count(f'"{name}"') for name in table)
    return {"users_md": users, "stance_slots": stances, "raw_mentions": raw_hits}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default="data")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    data = pathlib.Path(args.data)
    table = mapping(data)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    result = {"mode": "apply" if args.apply else "dry-run", "mapping": table}
    if table:
        result["facts"] = facts(data, table, apply=args.apply, stamp=stamp)
        result["threads"] = threads(data, table, apply=args.apply, stamp=stamp)
        result["proactive"] = proactive(data, table, apply=args.apply, stamp=stamp)
        result["report_only"] = report_only(data, table)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
