#!/usr/bin/env python3
"""只讀盤點（2026-11-15 WP2a）：TRPG／現實場景隔離修復前的兩類歷史髒資料。

用法（オーナー產 data 的副本；嚴格只讀，SQLite 以 mode=ro 開啟）：
    python3.12 agent/tools/migrations/audit_game_scene_20261115.py --data <data-copy> --copy-check
    python3.12 agent/tools/migrations/audit_game_scene_20261115.py --data <data-copy> --copy-check --out report.txt

盤點三類：
  1. 被寫成現實的桌上事實：
     `game=0` 且 `source ∈ {explicit, rule, self, explicit+mirror, rule+mirror, self+mirror}`。
     遍歷 `data/raw/<entity>/*.jsonl` 的 `role=user` 行，用 `facts_for_turn()` 重新生成該行會產生的事實
     （含同房間其他角色的 `+mirror`），按 `(normalize_text(text), entity)` 對照：
       · 只在 `game_of(source, purpose) == 1` 的行裡出現過 ⇒ 「確定是桌上的」（definite_table_as_real）
       · 在 `game_of == 1` 與 `game_of == 0` 的行裡都生成得出來 ⇒ 「有歧義」（ambiguous_table_as_real，不動）
  2. 跨場景取代：
     `superseded_by` 非空，且指向的目標行（同 `entity`、`key` 相同）與自己的 `game` 不同。
"""

from __future__ import annotations

import argparse
from collections import Counter
import datetime
import json
import pathlib
import resource
import sqlite3
import sys
import time
from typing import Iterable, Mapping

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.brain.domain.thread import MemoryScope, TurnRecord  # noqa: E402
from agent.brain.memory.fact_rules import facts_for_turn  # noqa: E402
from agent.brain.memory.facts import Fact, fact_key, game_of, join_keys, normalize_text  # noqa: E402
from agent.brain.runtime.memory import pointer as pointers  # noqa: E402
from agent.brain.runtime.rawlog import DEFAULT_KEEP_MONTHS, _shift_months  # noqa: E402

RULE_SOURCES = frozenset({
    "explicit", "rule", "self",
    "explicit+mirror", "rule+mirror", "self+mirror",
})
DEFAULT_ENTITIES = ("mira", "aster", "nori")


def _load_names(data_dir: pathlib.Path) -> dict[str, str]:
    """讀取主人的顯示名與 `guests.json`（若存在）裡的客人顯示名（只讀）。"""
    names: dict[str, str] = {"owner": "主人"}
    guests_path = data_dir / "guests.json"
    if not guests_path.is_file():
        return names
    try:
        raw = json.loads(guests_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return names
    items = raw.get("guests") if isinstance(raw, dict) else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("principal") and item.get("name"):
                names[str(item["principal"]).strip()] = str(item["name"]).strip()
    elif isinstance(raw, dict):
        for key, item in raw.items():
            if isinstance(item, dict) and item.get("name"):
                names[str(key).strip()] = str(item["name"]).strip()
    return names


def _scopes_for_row(row: Mapping[str, object], entity: str) -> list[MemoryScope]:
    speaker = str(row.get("speaker") or "").strip()
    scene = str(row.get("scene") or "together").strip() or "together"
    thread_id = str(row.get("thread") or "").strip()
    if bool(row.get("self_owned")) or speaker == "self":
        return [MemoryScope(principal="", entity=entity, scene=scene,
                            thread_id=thread_id, self_owned=True)]
    if speaker:
        return [MemoryScope(principal=speaker, entity=entity, scene=scene,
                            thread_id=thread_id, self_owned=False)]
    # 舊原文行若沒填 speaker，可能來自無主源（self_owned=True）或預設 owner
    return [
        MemoryScope(principal="", entity=entity, scene=scene,
                    thread_id=thread_id, self_owned=True),
        MemoryScope(principal="owner", entity=entity, scene=scene,
                    thread_id=thread_id, self_owned=False),
    ]


def _scan_raw_candidates(
    raw_dir: pathlib.Path,
    *,
    known_entities: set[str],
    names: Mapping[str, str],
) -> tuple[dict[tuple[str, str], list[str]], dict[tuple[str, str], list[str]], dict]:
    """掃描原文並回 `(game_turns, real_turns, coverage)`；只讀，不補寫任何檔案。"""
    game_turns: dict[tuple[str, str], list[str]] = {}
    real_turns: dict[tuple[str, str], list[str]] = {}
    paths = sorted(raw_dir.glob("*/*.jsonl")) if raw_dir.is_dir() else []
    coverage = {"files": len(paths), "lines": 0, "user_lines": 0,
                "game_lines": 0, "real_lines": 0, "malformed_lines": 0,
                "unreadable_files": 0, "earliest_at": None, "latest_at": None}
    name_maps: list[Mapping[str, str]] = [names, {}] if names else [{}]
    for path in paths:
        folder_entity = path.parent.name
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            coverage["unreadable_files"] += 1
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                coverage["lines"] += 1
                try:
                    row = json.loads(line)
                except ValueError:
                    coverage["malformed_lines"] += 1
                    continue
                if not isinstance(row, dict):
                    coverage["malformed_lines"] += 1
                    continue
                try:
                    at = float(row.get("at") or 0.0)
                except (TypeError, ValueError):
                    at = 0.0
                if at > 0:
                    coverage["earliest_at"] = at if coverage["earliest_at"] is None \
                        else min(float(coverage["earliest_at"]), at)
                    coverage["latest_at"] = at if coverage["latest_at"] is None \
                        else max(float(coverage["latest_at"]), at)
                if str(row.get("role") or "") != "user" or not str(row.get("text") or "").strip():
                    continue
                coverage["user_lines"] += 1
                entity = str(row.get("entity") or folder_entity).strip()
                turn = TurnRecord.from_dict(row)
                turn_id = str(row.get("turn") or "").strip() or f"{path.name}@{row.get('at', 0)}"
                room = [str(w).strip() for w in (row.get("room") or ()) if str(w).strip()]
                is_game = game_of(str(row.get("source") or ""), str(row.get("purpose") or "")) == 1
                coverage["game_lines" if is_game else "real_lines"] += 1
                target_map = game_turns if is_game else real_turns

                others = sorted({w for w in room if w in known_entities and w != entity})
                for scope in _scopes_for_row(row, entity):
                    for name_map in name_maps:
                        for fact in facts_for_turn(scope, turn, participants=room, names=name_map):
                            norm = normalize_text(fact.text)
                            if not norm:
                                continue
                            _record_turn(target_map, (norm, entity), turn_id)
                            if str(fact.layer) != "private":
                                for other in others:
                                    _record_turn(target_map, (norm, other), turn_id)
    return game_turns, real_turns, coverage


def _record_turn(mapping: dict[tuple[str, str], list[str]],
                 key: tuple[str, str], turn_id: str) -> None:
    bucket = mapping.setdefault(key, [])
    if turn_id and turn_id not in bucket:
        bucket.append(turn_id)


def _connect_readonly(db_path: pathlib.Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_fact_rows(db_file: pathlib.Path, jsonl_file: pathlib.Path) -> tuple[list[dict], str]:
    """優先以 SQLite mode=ro 讀取 facts.db；若本地只有正本 facts.jsonl 則只讀載入正本。"""
    if db_file.is_file():
        conn = _connect_readonly(db_file)
        try:
            rows = conn.execute(
                "SELECT id, entity, key, text, norm, source, created_at, "
                "COALESCE(game, 0) AS game, COALESCE(superseded_by, '') AS superseded_by, "
                "COALESCE(supersedes, '') AS supersedes, COALESCE(slot, '') AS slot, "
                "COALESCE(valid_from, 0) AS valid_from, COALESCE(valid_to, 0) AS valid_to "
                "FROM facts ORDER BY id ASC"
            ).fetchall()
            return [dict(r) for r in rows], f"{db_file} (sqlite mode=ro)"
        finally:
            conn.close()
    if jsonl_file.is_file():
        out: list[dict] = []
        with jsonl_file.open("r", encoding="utf-8", errors="replace") as handle:
            for idx, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(raw, dict) or not str(raw.get("text") or "").strip():
                    continue                    # R1 的 op 追加記錄不是事實，不計入總數
                fact = Fact.from_store_dict(raw)
                out.append({
                    "id": idx,
                    "entity": str(fact.entity or ""),
                    "key": str(fact.key or "") or fact_key(fact.text),
                    "text": str(fact.text or ""),
                    "norm": normalize_text(fact.text),
                    "source": str(fact.source or ""),
                    "created_at": float(fact.created_at or 0.0),
                    "game": int(fact.game or 0),
                    "superseded_by": str(fact.superseded_by or ""),
                    "supersedes": join_keys(fact.supersedes),
                    "slot": str(fact.slot or ""),
                    "valid_from": float(fact.valid_from or 0.0),
                    "valid_to": float(fact.valid_to or 0.0),
                })
        return out, f"{jsonl_file} (jsonl readonly fallback)"
    raise FileNotFoundError(f"找不到 {db_file} 或 {jsonl_file}")


def _fmt_time(ts: object) -> str:
    try:
        val = float(ts or 0.0)
    except (TypeError, ValueError):
        return "-"
    if val <= 0:
        return "0"
    dt = datetime.datetime.fromtimestamp(val, tz=datetime.timezone.utc)
    return f"{val:.3f} ({dt.strftime('%Y-%m-%d %H:%M:%S')}Z)"


def _row_turn_hint(text: str, entity: str,
                   game_turns: Mapping[tuple[str, str], list[str]],
                   real_turns: Mapping[tuple[str, str], list[str]]) -> str:
    extracted = pointers.extract(text)
    if extracted:
        return ",".join(extracted)
    key = (normalize_text(text), entity)
    turns = list(dict.fromkeys((game_turns.get(key) or []) + (real_turns.get(key) or [])))
    return ",".join(turns)


def audit_game_scene(
    data_dir: pathlib.Path | str,
    *,
    db_path: pathlib.Path | str | None = None,
    raw_dir: pathlib.Path | str | None = None,
    sample_limit: int = 20,
    keep_months: int = DEFAULT_KEEP_MONTHS,
    now: float | None = None,
) -> dict:
    """只讀盤點場景隔離資料；無法由現存原文解釋的規則事實另列，不視同乾淨。"""
    started = time.perf_counter()
    base = pathlib.Path(data_dir)
    db_file = pathlib.Path(db_path) if db_path is not None else (base / "memory" / "facts.db")
    jsonl_file = base / "memory" / "facts.jsonl"
    raw_root = pathlib.Path(raw_dir) if raw_dir is not None else (base / "raw")
    limit = max(1, int(sample_limit))
    rows, source_desc = _load_fact_rows(db_file, jsonl_file)

    known_entities = set(DEFAULT_ENTITIES)
    for row in rows:
        ent = str(row["entity"] or "").strip()
        if ent:
            known_entities.add(ent)
    if raw_root.is_dir():
        for child in raw_root.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                known_entities.add(child.name)

    names = _load_names(base)
    game_turns, real_turns, raw_coverage = _scan_raw_candidates(
        raw_root, known_entities=known_entities, names=names)
    stamp = time.time() if now is None else float(now)
    local = datetime.datetime.fromtimestamp(stamp)
    cutoff_year, cutoff_month = _shift_months(local.year, local.month, -max(1, int(keep_months)))
    raw_coverage.update(keep_months=max(1, int(keep_months)),
                        retention_cutoff=datetime.datetime(cutoff_year, cutoff_month, 1).timestamp())

    definite_table: list[dict] = []
    ambiguous_table: list[dict] = []
    unexplained: list[dict] = []
    for row in rows:
        game_val = int(row["game"] or 0)
        source = str(row["source"] or "").strip()
        if game_val != 0 or source not in RULE_SOURCES:
            continue
        entity = str(row["entity"] or "").strip()
        text = str(row["text"] or "")
        norm = str(row["norm"] or "").strip() or normalize_text(text)
        lookup = (norm, entity)
        g_turns = game_turns.get(lookup) or []
        r_turns = real_turns.get(lookup) or []
        entry = {
            "id": int(row["id"]), "entity": entity,
            "key": str(row["key"] or "").strip() or fact_key(text),
            "text": text, "source": source,
            "created_at": float(row["created_at"] or 0.0), "game": game_val,
            "turn": ",".join(g_turns), "real_turn": ",".join(r_turns),
        }
        if not g_turns and not r_turns:
            unexplained.append(entry)       # 顯示名改過、名册不同或原文已過保留期都可能落在這裡
        elif not g_turns:
            continue                        # 現實原文能解釋：乾淨
        elif r_turns:
            ambiguous_table.append(entry)
        else:
            definite_table.append(entry)

    by_entity_key: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        entity = str(row["entity"] or "").strip()
        row_key = str(row["key"] or "").strip() or fact_key(str(row["text"] or ""))
        if row_key:
            by_entity_key.setdefault((entity, row_key), []).append(row)

    cross_supersede: list[dict] = []
    for row in rows:
        sup = str(row["superseded_by"] or "").strip()
        if not sup:
            continue
        entity = str(row["entity"] or "").strip()
        row_game = int(row["game"] or 0)
        targets = by_entity_key.get((entity, sup)) or []
        mismatched = [target for target in targets if int(target["game"] or 0) != row_game]
        if not mismatched:
            continue
        target = mismatched[0]
        text = str(row["text"] or "")
        cross_supersede.append({
            "id": int(row["id"]), "entity": entity,
            "key": str(row["key"] or "").strip() or fact_key(text),
            "text": text, "source": str(row["source"] or ""),
            "created_at": float(row["created_at"] or 0.0), "game": row_game,
            "slot": str(row["slot"] or ""), "superseded_by": sup,
            "turn": _row_turn_hint(text, entity, game_turns, real_turns),
            "target_id": int(target["id"]), "target_game": int(target["game"] or 0),
            "target_source": str(target["source"] or ""),
            "target_created_at": float(target["created_at"] or 0.0),
            "target_text": str(target["text"] or ""),
        })

    peak_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform != "darwin":
        peak_rss *= 1024
    return {
        "db_path": source_desc, "raw_dir": str(raw_root), "total_facts": len(rows),
        "raw_game_patterns": len(game_turns), "raw_real_patterns": len(real_turns),
        "raw_coverage": raw_coverage,
        "definite_table_as_real": _summarize_category(definite_table, limit=limit),
        "ambiguous_table_as_real": _summarize_category(ambiguous_table, limit=limit),
        "unexplained_rule_facts": _summarize_category(unexplained, limit=limit),
        "cross_scene_supersede": _summarize_category(cross_supersede, limit=limit),
        "performance": {"elapsed_s": time.perf_counter() - started,
                        "process_peak_rss_bytes": peak_rss},
    }


def _summarize_category(items: Iterable[dict], *, limit: int) -> dict:
    rows = list(items)
    by_entity = dict(sorted(Counter(str(r.get("entity") or "") for r in rows).items()))
    return {
        "count": len(rows),
        "by_entity": by_entity,
        "samples": rows[:limit],
        "items": rows,
    }


def format_report(report: Mapping[str, object]) -> str:
    """將結構化盤點結果格式化為人類可讀報告。"""
    coverage = report.get("raw_coverage") or {}
    if not isinstance(coverage, Mapping):
        coverage = {}
    unexplained = report.get("unexplained_rule_facts") or {}
    unexplained_count = unexplained.get("count", 0) if isinstance(unexplained, Mapping) else 0
    earliest, latest = coverage.get("earliest_at"), coverage.get("latest_at")
    lines = [
        "# TRPG／現實記憶場景隔離 — 舊資料只讀盤點報告 (WP2a)",
        f"- 口徑：無法由現存原文解釋的規則事實={unexplained_count}；它不為 0 時，1a=0 不能解讀成「庫是乾淨的」。",
        f"- 資料庫：{report.get('db_path')}",
        f"- 原文目錄：{report.get('raw_dir')}",
        f"- 總事實數：{report.get('total_facts')}",
        f"- 原文覆蓋：檔案={coverage.get('files', 0)}、非空行={coverage.get('lines', 0)}、"
        f"可推導用戶行={coverage.get('user_lines', 0)}（桌上={coverage.get('game_lines', 0)} / "
        f"現實={coverage.get('real_lines', 0)}）、壞行={coverage.get('malformed_lines', 0)}",
        f"- 原文時間：最早={_fmt_time(earliest) if earliest else '-'} / "
        f"最晚={_fmt_time(latest) if latest else '-'}",
        f"- RawLog 保留期：{coverage.get('keep_months', DEFAULT_KEEP_MONTHS)} 個月；"
        f"本輪保留邊界={_fmt_time(coverage.get('retention_cutoff'))}；早於邊界的事實可能因原文已清理而無法解釋。",
        f"- 原文可推導規則事實型態數：桌上(game=1)={report.get('raw_game_patterns')} / "
        f"現實(game=0)={report.get('raw_real_patterns')}",
        "",
    ]
    sections = (
        ("1a. 被寫成現實的桌上事實（確定是桌上的）", report.get("definite_table_as_real") or {}),
        ("1b. 被寫成現實的桌上事實（有歧義，現實也出現過，不動）", report.get("ambiguous_table_as_real") or {}),
        ("1u. 無法由現存原文解釋的規則事實（不代表髒資料）", unexplained),
        ("2. 跨場景取代（superseded_by 指向不同 game）", report.get("cross_scene_supersede") or {}),
    )
    for title, sec in sections:
        if not isinstance(sec, Mapping):
            continue
        lines.append(f"## {title}")
        lines.append(f"- 總條數：{sec.get('count', 0)}")
        lines.append(f"- 按 entity 分布：{json.dumps(sec.get('by_entity') or {}, ensure_ascii=False)}")
        samples = sec.get("samples") or []
        if not samples:
            lines.append("- 樣例：（無）")
            lines.append("")
            continue
        lines.append(f"- 前 {len(samples)} 條樣例：")
        for idx, item in enumerate(samples, 1):
            lines.append(
                f"  [{idx}] id={item.get('id')} entity={item.get('entity')} "
                f"game={item.get('game')} source={item.get('source')} "
                f"created_at={_fmt_time(item.get('created_at'))} "
                f"turn={item.get('turn') or '-'}"
            )
            lines.append(f"      text: {item.get('text')}")
            if item.get("real_turn"):
                lines.append(f"      real_turn: {item.get('real_turn')}")
            if item.get("superseded_by"):
                lines.append(
                    f"      superseded_by={item.get('superseded_by')} -> "
                    f"target_id={item.get('target_id')} target_game={item.get('target_game')} "
                    f"target_source={item.get('target_source')} "
                    f"target_text={item.get('target_text')}"
                )
        lines.append("")
    performance = report.get("performance") or {}
    if isinstance(performance, Mapping):
        lines.append(f"- 耗時：{float(performance.get('elapsed_s') or 0.0):.3f}s；"
                     f"進程內存峰值：{int(performance.get('process_peak_rss_bytes') or 0) / 1048576:.1f} MiB")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只讀盤點 TRPG／現實記憶隔離歷史髒資料")
    parser.add_argument("--data", default=str(ROOT / "data"), help="生產 data 副本的目錄路徑")
    parser.add_argument("--db", default="", help="指定副本 facts.db（預設 <data>/memory/facts.db）")
    parser.add_argument("--raw", default="", help="指定副本 raw 目錄（預設 <data>/raw）")
    parser.add_argument("--copy-check", action="store_true",
                        help="確認輸入是生產庫副本；未確認或直接指向倉庫 data 時拒絕運行")
    parser.add_argument("--keep-months", type=int, default=DEFAULT_KEEP_MONTHS,
                        help="原文保留月數（預設 12；只影響報告邊界，不刪檔）")
    parser.add_argument("--sample-limit", type=int, default=20, help="每類輸出樣例上限（預設 20）")
    parser.add_argument("--json", action="store_true", help="輸出 JSON 格式（不含完整 items 重複陣列）")
    parser.add_argument("--out", default="", help="輸出報告至指定檔案（預設印到 stdout）")
    args = parser.parse_args(argv)
    if not args.copy_check:
        parser.error("請先複製生產 data，並用 --copy-check 明確確認輸入是副本")
    live = (ROOT / "data").resolve()
    targets = [pathlib.Path(args.data)]
    targets.extend(pathlib.Path(value) for value in (args.db, args.raw) if value)
    if any(path.resolve() == live or live in path.resolve().parents for path in targets):
        parser.error("禁止直接盤點倉庫內的生產 data；請在副本上運行")

    report = audit_game_scene(
        args.data, db_path=args.db or None, raw_dir=args.raw or None,
        sample_limit=args.sample_limit, keep_months=args.keep_months)
    if args.json:
        compact = dict(report)
        for key in ("definite_table_as_real", "ambiguous_table_as_real",
                    "unexplained_rule_facts", "cross_scene_supersede"):
            sec = dict(compact[key])
            sec.pop("items", None)
            compact[key] = sec
        output = json.dumps(compact, ensure_ascii=False, indent=2) + "\n"
    else:
        output = format_report(report)

    if args.out:
        out_path = pathlib.Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
