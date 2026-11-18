"""SQLite 記憶索引的建表結構與欄位遷移（2026-11-15 從 `memory_sqlite.py` 拆出，維持單檔 600 行棘輪）。

為什麼單獨拆出：建表 DDL、FTS5 觸發器與舊庫欄位演進（`entity`／時域／取代門牌／流／場景）屬於結構定義，
與 `SqliteMemory` 的讀寫檢索邏輯分開後，後續調整欄位或索引重建時不會再頂到 600 行上限。
"""

from __future__ import annotations

import re
import sqlite3

from agent.brain.memory.supersede import backfill_keys

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    norm       TEXT NOT NULL,           -- 正規化文字；**唯一性只在同一倉內**（見 UNIQUE）
    layer      TEXT NOT NULL,
    audience   TEXT,                    -- 逗号分隔；NULL＝世界
    label      TEXT NOT NULL DEFAULT '',
    principal  TEXT NOT NULL DEFAULT '',
    entity     TEXT NOT NULL DEFAULT '',   -- 哪個角色實體的倉（mira/aster/nori）；''＝舊資料
    consensus  INTEGER NOT NULL DEFAULT 0,
    self_owned INTEGER NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    kind       TEXT NOT NULL DEFAULT '',     -- preference｜state｜event｜rule
    valid_from REAL NOT NULL DEFAULT 0,      -- 事件時間（0＝用 created_at）
    valid_to   REAL NOT NULL DEFAULT 0,      -- 0＝仍有效
    superseded_by TEXT NOT NULL DEFAULT '',  -- 被哪一條取代（門牌：指向新的）
    supersedes TEXT NOT NULL DEFAULT '',     -- 它取代了哪幾條（門牌，逗號分隔：指向舊的）
    key        TEXT NOT NULL DEFAULT '',     -- 自己的門牌（normalize_text 的 sha1 前 16 位）
    slot       TEXT NOT NULL DEFAULT '',     -- 狀態槽位（同槽只有一個現值）
    utility    INTEGER NOT NULL DEFAULT 0,   -- 被撈出來且真的用進回覆幾次
    misses     INTEGER NOT NULL DEFAULT 0,   -- 被撈出來但沒用幾次
    last_used_at REAL NOT NULL DEFAULT 0,    -- 最後一次被使用的時間
    prior      REAL NOT NULL DEFAULT 0,      -- 寫入期把握度（0–1）
    shingles   TEXT NOT NULL DEFAULT '',
    stream     TEXT NOT NULL DEFAULT '',     -- 來源軸：dialogue｜life（owner 2026-11-03）
    topic      TEXT NOT NULL DEFAULT '',     -- 生活流的主題（preset key／活動標題）
    game       INTEGER NOT NULL DEFAULT 0,   -- 1＝TRPG 卓上（虛構）；0＝現實（owner 2026-11-11 B3）
    -- 去重的鍵是「同一倉裡同一句」：跨角色鏡像要把同一句話落進另一個倉（轉述用），
    -- 全域唯一會讓那次寫入直接 IntegrityError（2026-11-01 修）
    UNIQUE (norm, entity)
);
CREATE INDEX IF NOT EXISTS facts_layer ON facts(layer, created_at);
CREATE INDEX IF NOT EXISTS facts_entity ON facts(entity, created_at);

-- 注意力場的焦點窗（owner 2026-11-16「拆 fact_audience 表让焦点窗快速校准」）
-- `facts.audience` 是逗號串接的 TEXT、沒有索引 ⇒ 焦點窗只能全表 LIKE。拆成這張表之後
-- 「誰在受眾裡」是一條 (member, entity, created_at) 索引查詢；`kind='principal'` 那列
-- 讓 `principal ∈ focus` 也走同一條索引。索引只是投影：權威仍是 `facts.audience`。
CREATE TABLE IF NOT EXISTS fact_audience (
    fact_id    INTEGER NOT NULL,            -- facts.id
    member     TEXT NOT NULL,               -- 受眾成員或 principal（見 kind）
    kind       TEXT NOT NULL DEFAULT 'audience',  -- audience｜principal
    entity     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (fact_id, kind, member)
);
CREATE INDEX IF NOT EXISTS fact_audience_member
    ON fact_audience(member, entity, created_at DESC);
-- 投影要跟著刪（2026-11-16 審查返修）：`facts.id` 不是 AUTOINCREMENT，`clear()`／`rebuild()`／
-- `prune_memory` 刪掉之後號碼會被重用 ⇒ 留著的舊列會把**別人的事實**掛到這個人的焦點窗。
CREATE TRIGGER IF NOT EXISTS fact_audience_ad AFTER DELETE ON facts BEGIN
    DELETE FROM fact_audience WHERE fact_id = old.id;
END;

-- 焦點窗的另一半（2026-11-16 實測）：有了受眾表之後，`facts` 這一側還要能按
-- 「哪個倉＋哪一層＋時間」直接走索引。少了這條，查詢仍會先掃 `facts_entity` 的
-- 180 天範圍（mira：2.9 ms）；補上這條 ⇒ **0.32 ms（9×）**。
CREATE INDEX IF NOT EXISTS facts_entity_layer
    ON facts(entity, layer, created_at DESC);

-- 分流後的檢索主索引 `facts_stream(entity, stream, created_at)` **不在這裡建**：
-- 舊庫的 CREATE TABLE 是 no-op、還沒有 stream 欄 ⇒ 這支腳本會在補欄之前就炸
-- （`no such column: stream`，整個服務起不來）。改由 `migrate_temporal` 補完欄位後再建。
-- 為什麼不用 trigram、也不索引原文：中文要的是「二字詞」重疊（記憶體版那套 gram 語義）。
-- 把 norm 切成 bigram 用空白隔開存進 shingles ⇒ unicode61 會把每個 bigram 當一個 token，
-- 查詢側也切同樣的 bigram 做 OR ⇒ 命中語義與記憶體版一致，排序也沿用同一條公式。
CREATE TABLE IF NOT EXISTS facts_shingles (
    id INTEGER PRIMARY KEY REFERENCES facts(id) ON DELETE CASCADE,
    shingles TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    shingles, content='facts_shingles', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_shingles(id, shingles) VALUES (new.id, new.shingles);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    DELETE FROM facts_shingles WHERE id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    UPDATE facts_shingles SET shingles = new.shingles WHERE id = new.id;
END;
CREATE TRIGGER IF NOT EXISTS facts_sh_ai AFTER INSERT ON facts_shingles BEGIN
    INSERT INTO facts_fts(rowid, shingles) VALUES (new.id, new.shingles);
END;
CREATE TRIGGER IF NOT EXISTS facts_sh_ad AFTER DELETE ON facts_shingles BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, shingles) VALUES ('delete', old.id, old.shingles);
END;
CREATE TRIGGER IF NOT EXISTS facts_sh_au AFTER UPDATE ON facts_shingles BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, shingles) VALUES ('delete', old.id, old.shingles);
    INSERT INTO facts_fts(rowid, shingles) VALUES (new.id, new.shingles);
END;
"""


def norm_unique_is_global(db: sqlite3.Connection) -> bool:
    """這張表是不是還把 `norm` 當成全域唯一（舊 schema）。"""
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='facts'").fetchone()
    text = str(row["sql"] if row is not None else "")
    return bool(re.search(r"norm\s+TEXT\s+NOT\s+NULL\s+UNIQUE", text))


def migrate_temporal(db: sqlite3.Connection) -> None:
    """舊庫補時域欄位（SQLite 沒有 IF NOT EXISTS 加欄，只能查 pragma）。"""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(facts)")}
    for name, ddl in (("kind", "TEXT NOT NULL DEFAULT ''"),
                      ("valid_from", "REAL NOT NULL DEFAULT 0"),
                      ("valid_to", "REAL NOT NULL DEFAULT 0"),
                      ("superseded_by", "TEXT NOT NULL DEFAULT ''"),
                      # 取代關係的兩側指針（2026-11-01）：舊庫沒有 ⇒ 就地補
                      ("supersedes", "TEXT NOT NULL DEFAULT ''"),
                      ("key", "TEXT NOT NULL DEFAULT ''"),
                      ("slot", "TEXT NOT NULL DEFAULT ''"),
                      ("utility", "INTEGER NOT NULL DEFAULT 0"),
                      ("misses", "INTEGER NOT NULL DEFAULT 0"),
                      ("last_used_at", "REAL NOT NULL DEFAULT 0"),
                      ("prior", "REAL NOT NULL DEFAULT 0"),
                      # 來源軸與生活流主題（2026-11-03）：舊行先留空，
                      # 回填跑 `tools/backfill_streams.py`；留空＝讀的時候視同 dialogue
                      ("stream", "TEXT NOT NULL DEFAULT ''"),
                      ("topic", "TEXT NOT NULL DEFAULT ''"),
                      # 遊戲維度（2026-11-11 B3）：舊行留 0＝現實（沒打標過的就是沒打標）
                      ("game", "INTEGER NOT NULL DEFAULT 0"),
                      # 作品維度（2026-11-16）：1＝她寫出來的東西，0＝她親身經歷。
                      # 舊行一律 0；存量要重標時跑 `tools/migrations/backfill_fiction_20261116.py`
                      ("fiction", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            db.execute(f"ALTER TABLE facts ADD COLUMN {name} {ddl}")
    db.commit()
    for ddl in ("CREATE INDEX IF NOT EXISTS facts_stream"
                " ON facts(entity, stream, created_at)",
                # 作品 vs 經歷（2026-11-16）：欄位是上面才補的，索引一定得在這裡建
                "CREATE INDEX IF NOT EXISTS facts_fiction"
                " ON facts(fiction, entity, created_at DESC)"):
        try:
            db.execute(ddl)
            db.commit()
        except sqlite3.Error:
            pass
    # 舊行沒有門牌 ⇒ 現算一次（`memory/supersede.py::backfill_keys`）
    backfill_keys(db)


__all__ = ["SCHEMA", "migrate_temporal", "norm_unique_is_global"]
