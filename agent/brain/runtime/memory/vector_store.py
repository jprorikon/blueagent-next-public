"""向量倉：把事實／窗口摘要／原文的向量存在本地 SQLite（可重建、可離線）。

設計取捨：
  · **只存向量與鍵**，原文不複製（權威資料仍在 facts.db／windows／raw）
  · 用 `hash` 判斷要不要重算：文本沒變就不重嵌（模型換了 ⇒ 清表重建）
  · 一個角色一個 `entity` 分區（跟記憶分倉同一條線），檢索時只掃自己那份
  · 沒有 sqlite-vec 擴展也能跑：向量取回後在 Python 裡算餘弦（幾千條可接受）
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS vec (
    kind TEXT NOT NULL,          -- fact｜window｜turn
    entity TEXT NOT NULL,
    ref TEXT NOT NULL,           -- 事實正文／window_id／turn key
    hash TEXT NOT NULL,          -- 文本 sha1（變了才重嵌）
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vec TEXT NOT NULL,           -- JSON 陣列（可讀、可除錯；1024 維約 12KB）
    updated_at REAL NOT NULL,
    PRIMARY KEY (kind, entity, ref)
);
CREATE INDEX IF NOT EXISTS idx_vec_entity ON vec(entity, kind);
"""


def text_hash(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()


def cosine(a, b) -> float:
    """純 Python 餘弦（1024 維、幾千條沒問題；不引入 numpy 依賴）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


class VectorStore:
    def __init__(self, path, *, embedder=None, log=None) -> None:
        self.path = pathlib.Path(path)
        self.embedder = embedder
        self._lock = threading.RLock()
        self._log = log or (lambda _message: None)
        self.embedded = 0
        self.hits = 0
        #: 記憶體快取：`(kind, entity) -> [(ref, vec, norm)]`。**每層只載一次**，
        #: 否則每問一句都要把幾百條向量重新反序列化＋重算範數（實測 11 s/次）。
        self._cache: dict[tuple[str, str], list[tuple[str, list[float], float]]] = {}
        # 新裝（空的 data/）時 `memory/` 還不存在：以前是 demo 種子先寫 facts.jsonl 順手建了目錄，
        # 拿掉種子之後這裡就開不了庫 ⇒ 自己建（2026-10-25）
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ── 寫 ──────────────────────────────────────────────────────────────────
    def sync(self, kind: str, entity: str, items: list[tuple[str, str]],
             *, limit: int = 400) -> int:
        """`items` = [(ref, text)]；只嵌「新出現或文本改了」的那些。回本次嵌入數。"""
        if self.embedder is None or not items:
            return 0
        model = getattr(self.embedder, "model_name", "?")
        with self._lock:
            known = {row["ref"]: row["hash"] for row in self._db.execute(
                "SELECT ref, hash FROM vec WHERE kind = ? AND entity = ?", (kind, entity))}
        todo = [(ref, text) for ref, text in items
                if known.get(str(ref)) != text_hash(text)][:max(1, int(limit))]
        if not todo:
            return 0
        vectors = self.embedder.embed([text for _, text in todo], kind="passage")
        if not vectors:
            return 0
        now = time.time()
        rows = [(kind, entity, str(ref), text_hash(text), model, len(vector),
                 json.dumps([round(x, 6) for x in vector]), now)
                for (ref, text), vector in zip(todo, vectors)]
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO vec (kind, entity, ref, hash, model, dim, vec, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)", rows)
            self._db.commit()
            self.embedded += len(rows)
            self._cache.pop((str(kind), str(entity)), None)      # 有新向量就讓快取重載
        return len(rows)

    # ── 讀 ──────────────────────────────────────────────────────────────────
    def cached(self, kind: str, entity: str) -> list[tuple[str, list[float], float]]:
        """回 `[(ref, vec, norm)]`（有快取就用快取）。"""
        key = (str(kind), str(entity))
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        rows = []
        for ref, vec in self.load(kind, entity):
            norm = sum(value * value for value in vec) ** 0.5
            rows.append((ref, vec, norm))
        with self._lock:
            self._cache[key] = rows
        return rows

    def cached_only(self, kind: str, entity: str) -> dict[str, list[float]]:
        """只回**已經建好**的向量（缺的不補）——查詢路徑專用，絕不現場嵌入。"""
        return {ref: vec for ref, vec, _norm in self.cached(kind, entity)}

    def load(self, kind: str, entity: str) -> list[tuple[str, list[float]]]:
        with self._lock:
            rows = list(self._db.execute(
                "SELECT ref, vec FROM vec WHERE kind = ? AND entity = ?", (kind, entity)))
        out = []
        for row in rows:
            try:
                out.append((row["ref"], json.loads(row["vec"])))
            except ValueError:
                continue
        return out

    def counts(self) -> dict:
        with self._lock:
            rows = list(self._db.execute(
                "SELECT kind, COUNT(*) AS n FROM vec GROUP BY kind"))
        return {row["kind"]: int(row["n"]) for row in rows}

    def snapshot(self) -> dict:
        return {"path": str(self.path), "counts": self.counts(),
                "embedded": self.embedded,
                "embedder": self.embedder.snapshot() if self.embedder is not None else {}}
