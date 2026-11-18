#!/usr/bin/env python3
"""記憶庫清理（owner 2026-11-03「繼續清」）。

兩件事，各自可以用參數單獨跑：

  ① `--expired-life`：刪掉**已過期**的生活流條目（`self_note`／`activity_public`／
     `activity`／`self`）。它們的 TTL 是 7 天，過期後本來就不進檢索，
     只是躺著佔 500 MB；原文仍在 `data/logs/activity-*.jsonl`。
  ② `--near-duplicates`：對話流裡**同一件事被寫成好幾條**（「おやすみ」那種），
     用字面相似度聚類，留最新最長的一條，其餘 `supersede` 收掉（不刪，as-of 仍查得到）。

    python3 agent/tools/prune_memory.py                     # dry-run：兩件都只印計畫
    python3 agent/tools/prune_memory.py --expired-life --apply
    python3 agent/tools/prune_memory.py --near-duplicates --apply
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

#: 生活流裡「短命」的來源（過期即可刪；原文在日誌檔）
SHORT_LIVED = ("self_note", "activity_public", "activity", "self")
#: 近重複判據：語意 cosine（有向量時）或字面 bigram Jaccard（沒有時）
SIMILARITY = 0.90


def _db_path(data: pathlib.Path) -> pathlib.Path:
    return data / "memory" / "facts.db"


def expired_life(db: sqlite3.Connection, now: float) -> list[dict]:
    """已過期的生活流條目（TTL 過了 ⇒ 本來就不進檢索，只是佔空間）。"""
    marks = ",".join("?" for _ in SHORT_LIVED)
    rows = db.execute(
        f"SELECT id, entity, source, text FROM facts WHERE stream = 'life'"
        f" AND source IN ({marks}) AND valid_to > 0 AND valid_to < ?",
        (*SHORT_LIVED, now)).fetchall()
    return [{"id": int(row["id"]), "entity": str(row["entity"]),
             "source": str(row["source"]), "text": str(row["text"])[:50]} for row in rows]


def life_by_topic(db: sqlite3.Connection, *, keep: int = 20) -> list[dict]:
    """同一個主題只留最近 `keep` 條，其餘收掉（**不刪**，as-of 還查得到）。

    為什麼：她同一套預設活動會反覆做（aster 的 `observe-peers` 有 1,018 條），
    每一條都進生活流池 ⇒ 生活流自己內部就在互相擠。主題索引只需要「最近幾次」當實例。
    """
    rows = db.execute(
        "SELECT entity, topic, key, text, created_at FROM facts"
        " WHERE stream = 'life' AND topic != '' AND superseded_by = ''"
        " ORDER BY entity, topic, created_at DESC").fetchall()
    groups: dict[tuple[str, str], list] = {}
    for row in rows:
        groups.setdefault((str(row["entity"]), str(row["topic"])), []).append(row)
    out: list[dict] = []
    for (entity, topic), items in groups.items():
        if len(items) <= keep:
            continue
        keeper = items[0]
        doomed = items[keep:]
        out.append({"entity": entity, "topic": topic, "keep": str(keeper["text"])[:50],
                    "total": len(items), "drop": len(doomed),
                    "keys": [str(item["key"]) for item in doomed if str(item["key"])]})
    return out


def _subject_set(text: str) -> frozenset[str]:
    """句子裡出現的「主體」是誰——合併近重複時**主體不同就不合**。

    實測的坑：「主人怕冷，睡覺要蓋被子」與「助手怕冷，主人叮囑她蓋被子」字面幾乎一樣，
    語意卻相反。只看相似度會把這兩條併成一條，等於把錯的那條留下來。
    """
    marks = {"主人": ("主人", "オーナー", "オーナー", "owner"),
             "助手": ("助手", "彼女", "她怕", "她認為"),
             "訪客": ("客", "ゲスト", "guest")}
    return frozenset(name for name, words in marks.items() if any(word in text for word in words))


def _vectors(path: pathlib.Path) -> dict[tuple[str, str], list[float]]:
    """`(entity, ref) → 向量`（對話事實的語意向量；沒有就回空）。"""
    import json
    if not path.exists():
        return {}
    out: dict[tuple[str, str], list[float]] = {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for entity, ref, raw in db.execute("SELECT entity, ref, vec FROM vec WHERE kind = 'fact'"):
            try:
                out[(str(entity), str(ref))] = json.loads(raw)
            except ValueError:
                continue
    finally:
        db.close()
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def near_duplicates(db: sqlite3.Connection, *, vectors_path: pathlib.Path | None = None,
                    similarity: float | None = None) -> list[dict]:
    """同一角色、對話流、現行的條目裡，把「說的其實是同一件事」的聚成一組。

    有語意向量就用 cosine（≥ `SIMILARITY`），沒有就退回字面 bigram Jaccard。
    兩條硬條件：**主體必須一致**，長度不能差太多（避免把一句話併進一整段話）。
    """
    from agent.brain.memory.facts import grams
    threshold = SIMILARITY if similarity is None else float(similarity)
    vectors = _vectors(vectors_path) if vectors_path is not None else {}
    rows = db.execute("SELECT id, entity, key, text, created_at FROM facts"
                      " WHERE stream = 'dialogue' AND superseded_by = ''"
                      " ORDER BY entity, created_at").fetchall()
    groups: list[dict] = []
    by_entity: dict[str, list] = {}
    for row in rows:
        by_entity.setdefault(str(row["entity"]), []).append(row)
    for entity, items in by_entity.items():
        used: set[int] = set()
        for index, row in enumerate(items):
            if index in used:
                continue
            text = str(row["text"])
            cluster = [row]
            for other_index in range(index + 1, len(items)):
                if other_index in used:
                    continue
                other = items[other_index]
                other_text = str(other["text"])
                if _subject_set(text) != _subject_set(other_text):
                    continue                       # 主體不同（主人／助手）不合併
                shorter, longer = sorted((len(text), len(other_text)))
                if shorter / max(1, longer) < 0.55:
                    continue                       # 一句話 vs 一整段：不合併
                vec_a = vectors.get((entity, text))
                vec_b = vectors.get((entity, other_text))
                if vec_a is not None and vec_b is not None:
                    if _cosine(vec_a, vec_b) < threshold:
                        continue
                else:
                    grams_a, grams_b = grams(text), grams(other_text)
                    if not grams_a or not grams_b:
                        continue
                    if len(grams_a & grams_b) / max(1, len(grams_a | grams_b)) < threshold:
                        continue
                cluster.append(other)
                used.add(other_index)
            if len(cluster) > 1:
                # 留哪一條：① 不含**已廢棄的稱呼**（船長）優先，② 措辭最長，③ 最新
                keeper = max(cluster, key=lambda item: (
                    not any(word in str(item["text"]) for word in ("船長", "船长")),
                    len(str(item["text"])), float(item["created_at"])))
                groups.append({"entity": entity, "keep": str(keeper["text"])[:60],
                               "keys": [str(item["key"]) for item in cluster
                                        if item is not keeper and str(item["key"])],
                               "texts": [str(item["text"])[:38] for item in cluster]})
    return groups


def _backup(path: pathlib.Path, tag: str) -> pathlib.Path:
    backup = path.with_name(f"facts.db.bak-{tag}-{time.strftime('%Y%m%d')}")
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"已備份 → {backup.name}（{backup.stat().st_size / 1e6:.0f} MB）")
    return backup


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--expired-life", action="store_true")
    ap.add_argument("--near-duplicates", action="store_true")
    ap.add_argument("--life-by-topic", action="store_true")
    ap.add_argument("--keep-per-topic", type=int, default=20,
                    help="同一個主題留最近幾條（預設 20）")
    ap.add_argument("--similarity", type=float, default=None,
                    help="近重複門檻（預設 0.90；中文／日文措辭變體可降到 0.86 再跑一輪）")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    any_flag = args.expired_life or args.near_duplicates or args.life_by_topic
    do_life = args.expired_life or not any_flag
    do_dupes = args.near_duplicates or not any_flag
    do_topics = args.life_by_topic or not any_flag
    path = _db_path(pathlib.Path(args.data))
    if not path.exists():
        print(f"沒有 {path}")
        return 1

    now = time.time()
    db = sqlite3.connect(f"file:{path}" + ("" if args.apply else "?mode=ro"), uri=True)
    db.row_factory = sqlite3.Row

    if do_life:
        doomed = expired_life(db, now)
        print(f"① 過期的生活流：{len(doomed)} 條（TTL 已過 ⇒ 本來就不進檢索）")
        for row in doomed[:3]:
            print(f"   - {row['entity']} {row['source']} {row['text']}")
        if args.apply and doomed:
            db.close()
            _backup(path, "prune-life")
            db = sqlite3.connect(str(path))
            with db:
                db.executemany("DELETE FROM facts WHERE id = ?",
                               [(row["id"],) for row in doomed])
            db.close()
            check = sqlite3.connect(str(path))
            left = check.execute("SELECT count(*) FROM facts").fetchone()[0]
            check.close()
            print(f"   刪完：全庫剩 {left} 條")
            db = sqlite3.connect(f"file:{path}" + ("" if args.apply else "?mode=ro"), uri=True)
            db.row_factory = sqlite3.Row

    if do_dupes:
        groups = near_duplicates(db, vectors_path=path.with_name("vectors.db"),
                                 similarity=args.similarity)
        total = sum(len(group["keys"]) for group in groups)
        print(f"② 對話流的近重複：{len(groups)} 組，可收 {total} 條")
        for group in groups[:6]:
            print(f"   - [{group['entity']}] 留：{group['keep']}")
            for text in group["texts"]:
                print(f"        · {text}")
        if args.apply and total:
            db.close()
            _backup(path, "prune-dupes")
            from agent.brain.compose import wiring
            from agent.brain.memory.fact_store import FactStore
            base = pathlib.Path(args.data)
            memory = wiring.build_memory(base, store=FactStore(base / "memory" / "facts.jsonl"),
                                         names={}, social=None,
                                         entity_ids=("mira", "nori", "aster"))
            retired = 0
            for group in groups:
                if not group["keys"]:
                    continue
                retired += int(memory.supersede(entity=group["entity"], keys=group["keys"],
                                                new_key=group["keep"], at=now) or 0)
            close = getattr(memory, "close", None)
            if callable(close):
                close()
            print(f"   收掉 {retired} 條（標記 superseded，不刪）")

    if do_topics:
        groups = life_by_topic(db, keep=int(args.keep_per_topic))
        total = sum(group["drop"] for group in groups)
        print(f"③ 生活流按主題收斂（每主題留最近 {args.keep_per_topic} 條）："
              f"{len(groups)} 個主題、可收 {total} 條")
        for group in sorted(groups, key=lambda item: -item["drop"])[:6]:
            print(f"   - {group['entity']} {group['topic'][:24]}："
                  f"{group['total']} 條 → 留 {args.keep_per_topic}")
        if args.apply and total:
            db.close()
            _backup(path, "prune-topic")
            from agent.brain.compose import wiring as _wiring
            from agent.brain.memory.fact_store import FactStore as _FactStore
            _base = pathlib.Path(args.data)
            _memory = _wiring.build_memory(
                _base, store=_FactStore(_base / "memory" / "facts.jsonl"),
                names={}, social=None, entity_ids=("mira", "nori", "aster"))
            retired = 0
            for group in groups:
                if not group["keys"]:
                    continue
                retired += int(_memory.supersede(entity=group["entity"], keys=group["keys"],
                                                 new_key=group["keep"], at=now) or 0)
            _close = getattr(_memory, "close", None)
            if callable(_close):
                _close()
            print(f"   收掉 {retired} 條（標記 superseded；要復原用 memory.revive）")
            db = sqlite3.connect(f"file:{path}" + ("" if args.apply else "?mode=ro"), uri=True)
            db.row_factory = sqlite3.Row
    db.close()
    if not args.apply:
        print("（dry-run：什麼都沒改。要真的跑請加 --apply，會先備份 facts.db）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
