"""legacy outbox 的一次性只读导入；默认 dry-run，不产生社交事件。"""

from __future__ import annotations

import argparse
import json
import pathlib
import time

from app.server.chat.service import normalize_parts
from app.server.social.models import Actor


def read_latest_rows(path) -> tuple[list[dict], int]:
    latest = {}
    malformed = 0
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], 0
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not an object")
        except (ValueError, TypeError):
            malformed += 1
            continue
        key = (str(row.get("principal") or ""), str(row.get("entity") or ""),
               str(row.get("turn_id") or ""))
        if all(key):
            latest[key] = row                 # 对账追加的新版本覆盖旧版本
    return list(latest.values()), malformed


def _person(account) -> Actor:
    return account.actor() if account is not None else Actor("", "guest", "")


def _resolve_account(accounts, identity: str):
    """直接 ID 优先；旧 principal 只有唯一匹配时才可迁移，绝不按昵称猜测。"""
    return accounts.resolve_legacy(identity)


def _human_parts(row: dict) -> list[dict]:
    raw = row.get("asked_parts")
    if isinstance(raw, list) and raw:
        return normalize_parts(parts=raw)
    parts = []
    if str(row.get("asked_sticker") or ""):
        parts.append({"kind": "sticker", "sticker": str(row["asked_sticker"])})
    if str(row.get("asked") or "").strip():
        parts.append({"kind": "text", "text": str(row["asked"])})
    return parts


def _agent_parts(row: dict) -> list[dict]:
    raw = row.get("parts")
    if isinstance(raw, list) and raw:
        parts = normalize_parts(parts=[str(item) for item in raw if str(item).strip()])
    elif str(row.get("text") or "").strip():
        parts = normalize_parts(str(row["text"]))
    else:
        parts = []
    # 旧数组仅在没有英文正文标记时补充，避免同一贴纸出现两次。
    text = str(row.get("text") or "")
    if "[[sticker:" not in text:
        for item in row.get("stickers") or []:
            if isinstance(item, dict) and item.get("id"):
                parts.extend(normalize_parts(parts=[{"kind": "sticker", "sticker": str(item["id"])}]))
    for item in row.get("photos") or []:
        if isinstance(item, dict) and (item.get("id") or item.get("url")):
            parts.extend(normalize_parts(parts=[{
                "kind": "photo", "media_id": str(item.get("id") or ""),
                "url": str(item.get("url") or "")}]))
    return parts


def migrate_outbox(index_path, chat=None, accounts=None, *, apply=False, clock=time.time) -> dict:
    rows, malformed = read_latest_rows(index_path)
    stats = {"dry_run": not apply, "turns": 0, "messages": 0, "conversations": 0,
             "skipped": 0, "malformed": malformed, "conflicts": 0, "errors": []}
    conversations = set()
    planned = []
    for row in rows:
        principal = str(row.get("principal") or "")
        entity = str(row.get("entity") or "")
        turn_id = str(row.get("turn_id") or "")
        source = str(row.get("source") or "")
        kind = str(row.get("kind") or "")
        # 通话及其 call-log 仍属旧语音链路，不能导入后伪装成普通 Agent 文本泡泡。
        if not source.startswith(("phone", "api")) or kind in {"call", "call-log"}:
            stats["skipped"] += 1
            continue
        try:
            human = _human_parts(row)
            agent = _agent_parts(row)
            if not human and not agent:
                stats["skipped"] += 1
                continue
            stats["turns"] += 1
            stats["messages"] += bool(human) + bool(agent)
            conversations.add((principal, entity))
            planned.append((row, human, agent))
        except Exception as exc:  # 单行坏数据不挡其余导入；报告不写内部路径。
            stats["conflicts"] += 1
            stats["errors"].append({"turn_id": turn_id, "error": str(exc)[:120]})
    stats["conversations"] = len(conversations)
    if not apply:
        return stats
    if chat is None or accounts is None:
        raise ValueError("apply requires chat and accounts")
    imported = 0
    replays = 0
    for row, human, agent in planned:
        principal, entity = str(row["principal"]), str(row["entity"])
        turn_id = str(row["turn_id"])
        at = float(row.get("at") or clock())
        person_account = _resolve_account(accounts, principal)
        entity_account = _resolve_account(accounts, entity)
        if person_account is None or entity_account is None:
            stats["conflicts"] += 1
            stats["errors"].append({"turn_id": turn_id, "error": "unknown_account"})
            continue
        try:
            for direction, author, parts in (
                    ("asked", _person(person_account), human),
                    ("reply", _person(entity_account), agent)):
                if not parts:
                    continue
                _, created = chat.store.import_legacy(
                    principal=person_account.id, entity=entity_account.id,
                    turn_id=turn_id, direction=direction,
                    author=author, parts=parts, at=at, imported_at=clock())
                imported += int(created)
                replays += int(not created)
        except Exception as exc:
            stats["conflicts"] += 1
            stats["errors"].append({"turn_id": turn_id, "error": str(exc)[:120]})
    stats["imported"] = imported
    stats["idempotent_replays"] = replays
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=pathlib.Path, help="legacy data/outbox/index.jsonl（只读）")
    parser.add_argument("--apply", action="store_true", help="显式写入；省略时只输出 dry-run 对账")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps(migrate_outbox(args.index), ensure_ascii=False, indent=2))
        return 0
    from app.server.config import AppConfig
    from app.server.services import AppServices
    services = AppServices(AppConfig.from_env())
    try:
        print(json.dumps(migrate_outbox(args.index, services.chat, services.accounts,
                                        apply=True), ensure_ascii=False, indent=2))
    finally:
        services.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
