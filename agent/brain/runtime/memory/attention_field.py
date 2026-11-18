"""記憶注意力場（提案，owner 2026-10-31 提出、2026-11-16「帮我做了」）。

> 「比起直接權重，做一套注意力算法更重要。比如說當回主人會話的時候注意力更多放在和主人的
> 會話記憶上（模擬人在和人說話時回想上次和這人相關的內容）」

兩個動作（`docs/PLAN-memory-attention-20261031.md` §2.2／§2.3）：

1. **按焦點開窗（准入）**：候選池不只是「全庫按 created_at 取最新 600」——那條路今天只覆蓋
   12 小時、其中 98% 是她自己的生活流水，三天前的對話結構上就進不來。
   現在多一條：`layer ∈ {pair, private}` 且 `principal ∈ focus` 或 `audience ∋ focus` 的最近 N 條。
2. **注意力調制（排序）**：`score × gain`，
   `gain = 1 + α·overlap(audience, focus) + γ·in_session`。

三條硬約束（寫死在代碼裡）：

· **只調排序，不影響可見性**：`Fact.disclosable(present)` 在出結果前無條件執行，順序不得顛倒。
· **焦點窗只收關係層**（`pair`／`private`）：`self` 層（她自己的生活日誌，約 2.4 萬條）走底池，
  否則主人的窗會被自己的流水淹掉——正是 600 池子的病換個地方重演。
· **焦點為空**（她一個人：生活迴圈、prethink、反思）⇒ 焦點窗為空、候選＝底池，行為退回現況，
  不能變成「什麼都想不起來」。

β（stance 強度）留了接口但**預設 0**：`MemorySearch` 目前沒有 stance_store 的引用，
要接得像 `wiring` 那樣把 store 傳進來；先不假裝有這個數字。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

#: 焦點窗最多幾條（每個焦點成員）
FOCUS_CAP = int(os.environ.get("BLUEAGENT_FOCUS_CAP") or 120)
#: 焦點窗只看最近幾天（再早的對話靠底池或明確檢索）
FOCUS_DAYS = float(os.environ.get("BLUEAGENT_FOCUS_DAYS") or 180)
#: 關係層（只有這些層進焦點窗）
RELATION_LAYERS = ("pair", "private")
#: 調制係數
ALPHA = float(os.environ.get("BLUEAGENT_ATTENTION_ALPHA") or 0.35)   # 受眾重疊
GAMMA = float(os.environ.get("BLUEAGENT_ATTENTION_GAMMA") or 0.15)   # 同一段對話內
BETA = float(os.environ.get("BLUEAGENT_ATTENTION_BETA") or 0.1)      # stance（她→这个人有多近）
#: 什麼算「同一段對話」：與焦點最新一條記憶差這麼多秒以內
SESSION_WINDOW_S = float(os.environ.get("BLUEAGENT_ATTENTION_SESSION_S") or 6 * 3600)


def enabled() -> bool:
    return str(os.environ.get("BLUEAGENT_ATTENTION_FIELD") or "1").strip().lower() \
        not in {"0", "off", "false", "no"}


def focus_of(present, entity: str, *, limit: int = 4) -> set[str]:
    """這一輪「她該想起誰」＝在場的人（扣掉她自己）。"""
    people = {str(who).strip() for who in (present or ()) if str(who).strip()}
    people.discard(str(entity or "").strip())
    return set(sorted(people)[:limit])


_CONN: dict[str, sqlite3.Connection] = {}
_CONN_LOCK = threading.Lock()


#: 跟 `agent/adapter/fact_audience_index.BACKFILL_VERSION` 同值（runtime 不能 import adapter；
#: `test_fact_audience_index` 鎖兩邊一致）
_BACKFILL_VERSION = "1"


def _connect(db_path) -> sqlite3.Connection | None:
    """唯讀連線**重用**：每次查詢都開一個連線要 ~3 ms，焦點窗是每輪都跑的，不能這樣燒。"""
    key = str(db_path)
    with _CONN_LOCK:
        conn = _CONN.get(key)
        if conn is not None:
            return conn
    try:
        conn = sqlite3.connect(f"file:{key}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
    except sqlite3.Error:
        return None
    with _CONN_LOCK:
        _CONN[key] = conn
    return conn


def forget_connections() -> None:
    """測試用：關掉快取的連線（換庫／改 schema 後）。"""
    with _CONN_LOCK:
        for conn in _CONN.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        _CONN.clear()


def _index_ready(db: sqlite3.Connection) -> bool:
    """受眾索引**回填完整**才走索引；否則退回 LIKE 掃描（審核 R2，2026-11-17）。

    以前看「表裡有沒有任何一行」⇒ 升級後回填前寫進一條新事實就切過去，舊事實從焦點窗消失。
    現在只認 `fact_audience_meta` 的完整標記（`agent/adapter/fact_audience_index.py` 規則 5）。
    """
    if str(os.environ.get("BLUEAGENT_FOCUS_INDEX") or "1").strip().lower() in {"0", "off", "false", "no"}:
        return False
    try:
        row = db.execute("SELECT value FROM fact_audience_meta WHERE key='backfill'").fetchone()
    except sqlite3.Error:
        return False
    return row is not None and str(row[0]) == _BACKFILL_VERSION


def focus_rows(db_path, entity: str, focus, *, cap: int = FOCUS_CAP,
               days: float = FOCUS_DAYS, now: float | None = None) -> list[sqlite3.Row]:
    """焦點窗：關係層、且與焦點成員有關的最近幾條（**不含可見性判斷**，呼叫端無條件再過一次）。

    走 `fact_audience(member, entity, created_at)` 索引（owner 2026-11-16 拆表）；
    表還沒回填的舊庫自動退回 `audience LIKE` 掃描，行為一致。
    """
    people = [str(who) for who in (focus or ()) if str(who)]
    if not people or not db_path:
        return []
    moment = float(now if now is not None else time.time())
    since = moment - max(1.0, float(days)) * 86400.0
    layers = ",".join("?" for _ in RELATION_LAYERS)
    try:
        db = _connect(db_path)
        if db is None:
            return []
        marks = ",".join("?" for _ in people)
        if _index_ready(db):
            sql = (f"SELECT f.* FROM facts f JOIN fact_audience a ON a.fact_id = f.id "
                   f"WHERE f.entity = ? AND (f.superseded_by IS NULL OR f.superseded_by = '') "
                   f"AND f.layer IN ({layers}) AND f.created_at >= ? AND a.member IN ({marks}) "
                   f"GROUP BY f.id ORDER BY f.created_at DESC LIMIT ?")
            params = ([str(entity)] + list(RELATION_LAYERS) + [since] + people + [max(1, int(cap))])
        else:
            people_clause = " OR ".join(["principal = ?"] * len(people))
            audience_clause = " OR ".join(["audience LIKE ?"] * len(people))
            sql = (f"SELECT * FROM facts WHERE entity = ? AND (superseded_by IS NULL OR superseded_by = '') "
                   f"AND layer IN ({layers}) AND created_at >= ? AND ({people_clause} OR {audience_clause}) "
                   f"ORDER BY created_at DESC LIMIT ?")
            params = ([str(entity)] + list(RELATION_LAYERS) + [since]
                      + people + [f"%{who}%" for who in people] + [max(1, int(cap))])
        return db.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    # 連線共用（`_connect` 快取）⇒ 這裡不關


def _key(fact) -> tuple:
    """去重鍵：`Fact` 沒有 id（門牌是 `sha1(正規化文字)`）⇒ 用文字＋入庫時刻當鍵。"""
    return (str(getattr(fact, "text", "") or ""),
            round(float(getattr(fact, "created_at", 0) or 0.0), 3))


def merge(primary: list, extra: list) -> list:
    """底池 ∪ 焦點窗（同一條只留一份，順序：底池先）。"""
    seen = {_key(fact) for fact in primary}
    out = list(primary)
    for fact in extra:
        key = _key(fact)
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
    return out


def newest_focus_at(facts, focus) -> float:
    """焦點成員相關的記憶裡最新那一條的時間（給 `in_session` 用）。"""
    people = {str(who) for who in (focus or ()) if str(who)}
    newest = 0.0
    for fact in facts or ():
        audience = getattr(fact, "audience", None)
        principal = str(getattr(fact, "principal", "") or "")
        if principal in people or (audience and people & set(audience)):
            newest = max(newest, float(getattr(fact, "created_at", 0) or 0.0))
    return newest


def drop_fiction(pool: list) -> list:
    """去掉「她的作品」（`fiction=1`）：那是她寫的東西，不是她經歷的事（owner 2026-11-16）。

    預設就丟；要談創作（「我寫到港口那段」）時用 `BLUEAGENT_RECALL_FICTION=1` 放回來。
    """
    if str(os.environ.get("BLUEAGENT_RECALL_FICTION") or "0").strip().lower() not in {"0", "off", "false", "no"}:
        return pool
    return [fact for fact in pool if not int(getattr(fact, "fiction", 0) or 0)]


def stance_strength(service, entity: str, focus) -> float:
    """β 項的輸入：「她→焦點裡的人」的關係強度（0–1）。焦點多人時取最強的那個。

    `service` 是 `StanceService`（`runtime/stance.py`）；沒有就回 0（β 項消失＝舊行為）。
    """
    if service is None or not focus:
        return 0.0
    best = 0.0
    for member in focus:
        try:
            stance = service.get(str(entity), str(member))
            axes = list(getattr(stance, "axes", ()) or ())
        except Exception:                                  # noqa: BLE001 - 取不到就當沒有
            continue
        if not axes:
            continue
        mean = sum(float(getattr(axis, "weight", 0.0) or 0.0) for axis in axes) / len(axes)
        best = max(best, max(0.0, min(1.0, mean / 100.0)))
    return round(best, 3)


def gain(fact, focus, *, newest_at: float = 0.0, stance_strength: float = 0.0) -> tuple[float, dict]:
    """`1 + α·overlap + β·stance + γ·in_session`。

    `stance_strength` 是「她→焦点里的人」的关系强度（0–1，由 `MemorySearch` 从 StanceService 取），
    只在**这条事实确实和那个人有关**（overlap>0）时才乘——否则世界事实会被关系强度连带加权。
    """
    people = {str(who) for who in (focus or ()) if str(who)}
    if not people:
        return 1.0, {"overlap": 0.0, "session": 0.0, "stance": 0.0, "gain": 1.0}
    audience = getattr(fact, "audience", None)
    shared = people & set(audience) if audience else set()
    overlap = len(shared) / max(1, len(people))
    principal = str(getattr(fact, "principal", "") or "")
    involved = bool(shared) or principal in people
    at = float(getattr(fact, "created_at", 0) or 0.0)
    # 「同一段對話」只對**和這個人有關**的記憶有意義：世界事實／她自己的流水不加這段
    in_session = 1.0 if (involved and newest_at and abs(newest_at - at) <= SESSION_WINDOW_S) else 0.0
    stance = max(0.0, min(1.0, float(stance_strength or 0.0))) if involved else 0.0
    value = 1.0 + ALPHA * overlap + BETA * stance + GAMMA * in_session
    return value, {"overlap": round(overlap, 3), "session": in_session,
                   "stance": round(stance, 3), "gain": round(value, 3)}


def extend(db_path, entity: str, focus, pool: list, *, to_fact=None, readable=None,
           game: int | None = None) -> list:
    """把焦點窗併進候選池（`to_fact` 轉 Fact、`readable` 是硬約束：披露規則無條件執行）。

    `game` 必須一起傳：桌上／現實是兩套記憶（2026-11-11 B4），焦點窗不能把桌上那條
    撈進現實回合（測試 `test_memory_game_scene_isolation` 守著這條）。
    """
    if to_fact is None or not focus:
        return pool
    extra = []
    for row in focus_rows(db_path, entity, focus):
        try:
            if game is not None and int(row["game"] or 0) != int(game):
                continue
            fact = to_fact(row)
            if str(getattr(fact, "superseded_by", "") or ""):
                continue
            if not fact.is_active():
                continue
            if readable is not None and not readable(fact):
                continue
        except Exception:                                  # noqa: BLE001 - 壞行跳過
            continue
        extra.append(fact)
    return merge(pool, extra)


__all__ = ["ALPHA", "BETA", "GAMMA", "FOCUS_CAP", "FOCUS_DAYS", "RELATION_LAYERS", "SESSION_WINDOW_S",
           "enabled", "extend", "focus_of", "focus_rows", "gain", "merge", "newest_focus_at"]
