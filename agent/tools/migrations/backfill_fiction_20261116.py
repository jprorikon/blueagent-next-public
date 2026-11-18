#!/usr/bin/env python3
"""存量分「作品 vs 經歷」（owner 2026-11-16 選的第 3 件）。

背景：mira 22:52 的私聊「港の灯りを数えてた」來自她寫的場景（`work-scenario` 活動），
卻被當成親身經歷。新寫入已按活動的 `fiction` 標記落庫；**存量**要靠這支重標。

預設**只讀盤點**：按 `topic`（活動／preset）分組，把「看起來是作品」的群組列出來給人挑。
`--apply` 才寫，且**只改你點名的那幾組**（`--topics`），不會全庫亂標。

    # ① 盤點（不動資料）
    python3 agent/tools/migrations/backfill_fiction_20261116.py --data /app/data
    # ② 點名套用（例：寫場景那套活動）
    python3 agent/tools/migrations/backfill_fiction_20261116.py --data /app/data \
        --apply --topics work-scenario --topics "卓で使える短い場面を一つ書く"
    # ③ 驗收：被標記之後，主動搭話的素材裡不該再出現那些場景
    python3 agent/tools/migrations/backfill_fiction_20261116.py --data /app/data --verify

規則：只改 `facts.fiction`（1＝作品），**不動任何文字、不刪行**；可重跑（第二次是 0 筆）。
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 只盤點這幾種來源（`seed`/`manual`/`distill` 不動）
SOURCES = ("self_note", "activity_summary", "activity_public", "reflection")


def _db(data_dir: pathlib.Path) -> sqlite3.Connection:
    path = data_dir / "memory" / "facts.db"
    if not path.exists():
        raise SystemExit(f"找不到 {path}")
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row          # migrate_temporal 需要 row["name"]
    # 舊庫還沒有 fiction 欄 ⇒ 先補（與開機時跑的是同一支，可重複）
    from agent.adapter.memory_sqlite_schema import migrate_temporal
    migrate_temporal(db)
    return db


def inventory(db: sqlite3.Connection, *, entity: str = "", limit: int = 40) -> list[tuple]:
    marks = ",".join("?" for _ in SOURCES)
    sql = (f"SELECT COALESCE(NULLIF(topic,''),'(no-topic)') AS grp, source, "
           f"SUM(CASE WHEN COALESCE(fiction,0)=0 THEN 1 ELSE 0 END) AS todo, "
           f"SUM(CASE WHEN COALESCE(fiction,0)=1 THEN 1 ELSE 0 END) AS done "
           f"FROM facts WHERE source IN ({marks})")
    params: list = list(SOURCES)
    if entity:
        sql += " AND entity = ?"
        params.append(entity)
    sql += " GROUP BY grp, source ORDER BY todo DESC LIMIT ?"
    params.append(int(limit))
    return db.execute(sql, params).fetchall()


def apply_text(db: sqlite3.Connection, phrases: list[str], *, entity: str = "") -> dict:
    """按**文字內容**標（給 topic 為空的舊資料用：活動名寫在正文裡的，例如「卓で使える短い場面を一つ書く」）。"""
    total = 0
    for phrase in phrases:
        sql = ("UPDATE facts SET fiction = 1 WHERE COALESCE(fiction,0) = 0 "
               "AND source IN (" + ",".join("?" for _ in SOURCES) + ") AND text LIKE ?")
        params: list = list(SOURCES) + [f"%{phrase}%"]
        if entity:
            sql += " AND entity = ?"
            params.append(entity)
        cur = db.execute(sql, params)
        total += int(cur.rowcount or 0)
    db.commit()
    return {"marked": total}


def apply_reflection_days(db: sqlite3.Connection, *, entity: str = "") -> dict:
    """那天她寫了作品 ⇒ 那天的**日記**也不算經歷（`reflection` 由當天活動收成）。

    判定用同一張表：某一天的 `work-scenario` 類事實已經標成 fiction=1 ⇒ 把那天寫的
    `reflection` 也標成作品。沒有作品的那天不受影響。
    """
    sql = ("SELECT DISTINCT date(created_at,'unixepoch','localtime') FROM facts "
           "WHERE COALESCE(fiction,0)=1")
    days = [row[0] for row in db.execute(sql)]
    total = 0
    for day in days:
        # 日記在**隔天凌晨**（00:17）才寫，所以同時比對「當天」與「隔天」
        q = ("UPDATE facts SET fiction = 1 WHERE COALESCE(fiction,0)=0 AND source='reflection' "
             "AND (date(created_at,'unixepoch','localtime') = ? "
             "OR date(created_at,'unixepoch','localtime') = date(?,'+1 day'))")
        params: list = [day, day]
        if entity:
            q += " AND entity = ?"
            params.append(entity)
        cur = db.execute(q, params)
        total += int(cur.rowcount or 0)
    db.commit()
    return {"marked": total, "days": len(days)}


def apply(db: sqlite3.Connection, topics: list[str], *, entity: str = "") -> dict:
    marks = ",".join("?" for _ in SOURCES)
    place = ",".join("?" for _ in topics)
    sql = (f"UPDATE facts SET fiction = 1 WHERE COALESCE(fiction,0) = 0 "
           f"AND source IN ({marks}) AND COALESCE(NULLIF(topic,''),'(no-topic)') IN ({place})")
    params: list = list(SOURCES) + topics
    if entity:
        sql += " AND entity = ?"
        params.append(entity)
    cur = db.execute(sql, params)
    db.commit()
    return {"marked": int(cur.rowcount or 0)}


def verify(db: sqlite3.Connection, *, entity: str = "") -> dict:
    """被標成作品的群組，抽 3 條看內容（人眼確認不是她的真實經歷）。"""
    sql = ("SELECT topic, substr(text,1,80) FROM facts WHERE COALESCE(fiction,0)=1 "
           "AND source IN (" + ",".join("?" for _ in SOURCES) + ")")
    params: list = list(SOURCES)
    if entity:
        sql += " AND entity = ?"
        params.append(entity)
    sql += " ORDER BY created_at DESC LIMIT 6"
    rows = db.execute(sql, params).fetchall()
    total = db.execute("SELECT COUNT(*) FROM facts WHERE COALESCE(fiction,0)=1").fetchone()[0]
    return {"fiction_total": int(total), "samples": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data")
    parser.add_argument("--entity", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--topics", action="append", default=[])
    parser.add_argument("--text-contains", action="append", default=[],
                        help="按正文關鍵字標（topic 為空的舊資料用）")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--reflection-days", action="store_true",
                        help="把「寫過作品那一天」的日記也標成作品")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    data_dir = pathlib.Path(args.data)
    db = _db(data_dir)
    try:
        print(f"# 存量盤點（{'entity=' + args.entity if args.entity else '全部角色'}）"
              f"　來源限 {', '.join(SOURCES)}")
        rows = inventory(db, entity=args.entity, limit=args.limit)
        print(f"{'topic':<40} {'source':<16} {'待標':>6} {'已標':>6}")
        for topic, source, todo, done in rows:
            print(f"{str(topic)[:40]:<40} {str(source):<16} {todo:>6} {done:>6}")
        if args.apply and args.text_contains:
            result = apply_text(db, args.text_contains, entity=args.entity)
            print(f"\n按正文關鍵字標記 {result['marked']} 條為作品")
        if not args.apply:
            print("\n（只讀。要真的標：加 --apply --topics <上面某個 topic>）")
        elif args.apply and args.topics:
            result = apply(db, args.topics, entity=args.entity)
            print(f"\n已標記 {result['marked']} 條為作品（fiction=1）")
        if args.apply and args.reflection_days:
            result = apply_reflection_days(db, entity=args.entity)
            print(f"\n寫過作品的日子有 {result['days']} 天；這些天的日記標了 {result['marked']} 條")
        if args.verify:
            report = verify(db, entity=args.entity)
            print(f"\n驗收：全庫作品 {report['fiction_total']} 條；最近 6 條抽樣：")
            for topic, text in report["samples"]:
                print(f"  [{topic}] {text}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
