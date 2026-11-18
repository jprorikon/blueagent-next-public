#!/usr/bin/env python3
"""前史（初期記憶）を記憶庫に入れる——**一度だけ**走らせる道具（owner 2026-11-03）。

なぜ要るか：人格檔の「記憶（前史）」は毎回 prompt に載るが、**記憶庫には入っていない**。
すると彼女は前史を「知っている」のに、記憶を辿る問い（「前に話したあの話」）では出てこない。
外部専案 Starlit の「キャラが最初から記憶を持っている」に対応するのがこの層。

安全側の設計：
  · 既定は **dry-run**（何も書かない）。`--apply` で初めて書く。
  · 書くのは**追加だけ**（`memory.add`）。既存の記憶は消さない・書き換えない。
  · 同じ文が既にその役の倉にあれば**飛ばす**（`norm + entity` で判定）⇒ 何度走らせても増えない。
  · `source='seed'` を付ける ⇒ あとから `WHERE source='seed'` で見つけられる／外せる。
  · **相手（オーナー）との出来事は入れない**：実際には起きていないことを彼女の記憶にすると、
    捏造になる（SOUL.md §8）。入るのは彼女ひとり／第三者との過去だけ。

使い方（コンテナの中で）：
    python3 agent/tools/seed_initial_memories.py                    # 三人ぶんの予定を表示
    python3 agent/tools/seed_initial_memories.py --entity aster        # 一人だけ
    python3 agent/tools/seed_initial_memories.py --apply             # 実行

元データ：`data/persona/<entity>.initial_memories.json`
    {"events": [{"text": "…", "days_ago": 880, "kind": "event", "layer": "self"}, …]}
    `days_ago` は**イベント時間**（`valid_from`）。入庫時間（`created_at`）は「いま」——
    うちの二時域の規約どおり、過去の出来事を今日入れた、という扱いにする。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

ROOT = pathlib.Path(__file__).resolve().parents[2]
SUFFIX = ".initial_memories.json"
ENTITIES = ("mira", "nori", "aster")


def _log(message: str) -> None:
    print(message, flush=True)


# ── 讀 ────────────────────────────────────────────────────────────────────────
def events_of(path: pathlib.Path) -> list[dict]:
    """`[{text, days_ago, kind, layer}, …]`；壞檔回空 list（不擋其他角色）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = raw.get("events") if isinstance(raw, dict) else None
    out: list[dict] = []
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        try:
            days = float(row.get("days_ago") or 0)
        except (TypeError, ValueError):
            days = 0.0
        out.append({"text": text, "days_ago": max(0.0, days),
                    "kind": str(row.get("kind") or "event"),
                    "layer": str(row.get("layer") or "self")})
    return out


def _norm(text: str) -> str:
    """與記憶庫同一個正規化（`memory/facts.normalize_text`）——比對靠它才對得上。"""
    from agent.brain.memory.facts import normalize_text
    return normalize_text(text)


def existing_norms(db_path: pathlib.Path, entity: str) -> set[str]:
    """那個役的倉裡已有的 `norm`（唯讀；庫不存在就當空的）。"""
    if not db_path.exists():
        return set()
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        rows = db.execute("SELECT norm FROM facts WHERE entity = ?", (str(entity),)).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        db.close()
    return {str(row[0]) for row in rows if row and row[0]}


# ── 計畫 ──────────────────────────────────────────────────────────────────────
def plan(*, data: pathlib.Path, entities=ENTITIES, now: float | None = None) -> list[dict]:
    """要寫／要跳過的清單（純函式，測試直接吃它）。"""
    stamp = float(now if now is not None else time.time())
    db_path = data / "memory" / "facts.db"
    out: list[dict] = []
    for entity in entities:
        path = data / "persona" / f"{entity}{SUFFIX}"
        rows = events_of(path)
        have = existing_norms(db_path, entity)
        for row in rows:
            key = _norm(row["text"])
            out.append({"entity": entity, "text": row["text"], "kind": row["kind"],
                        "layer": row["layer"], "valid_from": stamp - row["days_ago"] * 86400.0,
                        "skip": bool(key) and key in have, "source_file": path.name})
    return out


def _fmt(when: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(when))


# ── 寫 ────────────────────────────────────────────────────────────────────────
def apply(data: pathlib.Path, rows: list[dict], *, entities=ENTITIES) -> int:
    """生產同一條路（`wiring.build_memory`＋`memory.add`）把沒有的那幾條寫進去。回寫入數。"""
    from agent.brain.compose import wiring
    from agent.brain.memory.fact_store import FactStore

    # ⚠️ 2026-11-03 實測踩到：這個變數只在容器 env 裡（compose 寫 `BLUEAGENT_MEMORY: sqlite`），
    #    在 host 上跑時是空的 ⇒ `build_memory` 落到 **InMemoryMemory**，`add()` 只 append 到
    #    `facts.jsonl`，索引庫一條都沒進（跑完印「寫入 21 條」，庫裡 0 條）。生產就是 sqlite，
    #    沒指定時就當 sqlite；真的要走別的後端請自己 export。
    if not str(os.environ.get("BLUEAGENT_MEMORY") or "").strip():
        os.environ["BLUEAGENT_MEMORY"] = "sqlite"
        _log("  （BLUEAGENT_MEMORY 未設 ⇒ 當作 sqlite，與 compose 一致）")
    memory = wiring.build_memory(data, store=FactStore(data / "memory" / "facts.jsonl"),
                                 names={}, social=None, entity_ids=entities)
    if type(memory).__name__ == "InMemoryMemory":
        raise SystemExit("記憶後端是 InMemoryMemory：寫了只會進 facts.jsonl，索引庫不會有。中止。")
    written = 0
    try:
        for row in rows:
            if row["skip"]:
                continue
            memory.add(row["text"], layer=row["layer"], label="前史", entity=row["entity"],
                       self_owned=True, source="seed", kind=row["kind"],
                       valid_from=row["valid_from"], prior=0.9)
            written += 1
    finally:
        close = getattr(memory, "close", None)
        if callable(close):
            close()
    return written


def verify(data: pathlib.Path) -> list[tuple[str, int]]:
    """驗收：庫裡 `source='seed'` 各幾條（寫完自己數，不信「寫入 N 條」那行）。"""
    db_path = data / "memory" / "facts.db"
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = db.execute("SELECT entity, count(*) FROM facts WHERE source = 'seed'"
                          " GROUP BY entity ORDER BY entity").fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    return [(str(row[0]), int(row[1])) for row in rows]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("BLUEAGENT_DATA_DIR") or str(ROOT / "data"))
    ap.add_argument("--entity", action="append", choices=list(ENTITIES),
                    help="指定角色（可重複；預設三人全做）")
    ap.add_argument("--apply", action="store_true", help="真的寫進記憶庫（預設只印計畫）")
    a = ap.parse_args(argv)

    data = pathlib.Path(a.data)
    entities = tuple(a.entity) if a.entity else ENTITIES
    rows = plan(data=data, entities=entities)
    if not rows:
        _log(f"沒有可讀的前史檔（{data}/persona/<entity>{SUFFIX}）⇒ 什麼都不做")
        return 1
    add_n = sum(1 for row in rows if not row["skip"])
    _log(f"前史：{len(rows)} 條（未入庫 {add_n}／已存在 {len(rows) - add_n}）"
         f"　data={data}　mode={'apply' if a.apply else 'dry-run'}")
    for row in rows:
        mark = "skip" if row["skip"] else "add "
        _log(f"  [{mark}] {row['entity']:8s} {_fmt(row['valid_from'])}  {row['text'][:56]}")
    if not a.apply:
        _log("（dry-run：什麼都沒寫。要真的寫入請加 --apply）")
        return 0
    written = apply(data, rows, entities=entities)
    got = verify(data)
    _log(f"寫入 {written} 條；庫裡 source='seed' 現有："
         + ("、".join(f"{name} {count}" for name, count in got) if got else "（沒有！）"))
    _log("回滾：DELETE FROM facts WHERE source='seed' 並重跑（索引只是一份投影）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
