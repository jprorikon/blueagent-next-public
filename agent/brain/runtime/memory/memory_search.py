"""`memory.search`（P3）：她自己在回合裡查過去，而不是開頭被動塞。

三層一次查完，各自守各自的披露規則：

| 層 | 來源 | 披露規則 |
|---|---|---|
| 事實 | `facts`（SQLite／記憶體） | `Fact.disclosable(在場的人)`：`在場 ⊆ audience` |
| 窗口摘要 | `window_summaries`（提煉產物） | `在場 ⊆ 那段原文的房間成員`（取交集；找不到原文就不說） |
| 原文 | `data/raw/<entity>/` | `在場 ⊆ 那次房間成員` |

三層都是同一條子集規則，方向一律是「**在場的人 ⊆ 房間**」（DESIGN §6.2）。
2026-10-24 之前窗口／原文兩層寫反成 `房間 ⊆ 在場`：主人跟 alice 同時在場時，
主人 1:1 的原文會被撈出來（多一個房間外的人反而更容易通過）。

「在場」只有兩三個人時，這條子集規則會**擋掉大部分跨房間內容**——這是刻意的（她記得，但不說）。

檢索方式：**混合**（owner 2026-10-23「直接 3」）——語意（本機 embedding）＋字面 bigram ＋
時間衰減 ＋ 層加權。原本只做 bigram，於是「ラノベ／小説／小说／novel」互相搜不到；
Hindsight 那條語意線又因 `192.168.50.36:8897` 不在線而全程靜默失敗。
"""

from __future__ import annotations

import json
import os
import math
import sqlite3
import time

from agent.brain.memory.facts import game_of, grams
from agent.brain.runtime.memory import attention_field
from agent.brain.runtime.memory import pointer as pointers
from agent.brain.runtime.memory.disclosure import disclosable as _disclosable
from agent.brain.runtime.memory.disclosure import readable as _readable
from agent.brain.runtime.memory.memory_open import open_pointer
from agent.brain.runtime.memory.vector_store import cosine
from agent.brain.runtime.memory.fact_history import memory_db_path
from agent.brain.runtime.memory.life_index import life_topics
from agent.brain.runtime.memory.past_topic import scope_superseded
from agent.brain.runtime.memory.time_intent import AS_OF_HINTS, PAST_HINTS, time_intent  # noqa: F401
from agent.brain.runtime.memory.window_store import WindowStore, window_age, window_room as _window_room

#: 混合排序權重（語意為主、字面為輔；時間衰減只在窗口層額外乘）
W_SEMANTIC = 0.60
W_LEXICAL = 0.25
W_RECENCY = 0.15
#: 語意門檻：低於這條且字面也 0 重疊 ⇒ 視為沒命中（避免「什麼都回一點」）
#: 實測（jina-clip-v2，Mac mini）：相關 0.62–0.92（小説↔ラノベ 0.919、カップ麺↔泡面 0.719）；
#: 無關 0.43。⇒ 0.56 有明確間隔；換模型時用 env 覆寫（`BLUEAGENT_SEMANTIC_FLOOR`）。
#: 有 rerank 時用**候選門檻**（放寬，讓邊緣候選進來給 LLM 判）；沒有 rerank 才用嚴格門檻
CANDIDATE_FLOOR = float(os.environ.get("BLUEAGENT_CANDIDATE_FLOOR") or 0.45)
#: 有字面命中就不看這條；**純語意命中**要更高的分（實測：真命中 0.62–0.67、雜訊 0.59）
SEMANTIC_FLOOR = float(os.environ.get("BLUEAGENT_SEMANTIC_FLOOR") or 0.62)
# 「過去意圖」詞與時間點（相對說法／N 天前／具體日期）住在 `time_intent.py`；舊名字留給既有 import
#: 原文層候選：語意取前 N、字面取前 M（全量掃描只做 bigram，成本低）
TURN_SEMANTIC_TOP = int(os.environ.get("BLUEAGENT_TURN_SEMANTIC_TOP") or 40)
TURN_LEXICAL_TOP = int(os.environ.get("BLUEAGENT_TURN_LEXICAL_TOP") or 40)
#: 一次檢索的預算（秒）：超過就不再查下一層（寧可少回，也不要讓她回話卡住）
SEARCH_BUDGET_S = float(os.environ.get("BLUEAGENT_SEARCH_BUDGET_S") or 2.0)
#: 相對門檻：跟當層最高語意分差超過這個量就不收（同義詞之間本來就擠在一起）
SEMANTIC_MARGIN = float(os.environ.get("BLUEAGENT_SEMANTIC_MARGIN") or 0.03)
#: 層加權（與 sqlite 版一致，避免兩邊排序漂）
LAYER_BOOST = {"consensus": 0.06, "circle": 0.10, "pair": 0.12, "private": 0.08}

#: 窗口摘要的時間衰減半衰期（天）。同一件事，剛聊過的應該比三週前的靠前。
WINDOW_HALF_LIFE_DAYS = 14.0

#: 重排模式（owner 2026-10-24：「把重排從讀路搬到寫路」）
#:   read  ＝ 讀路叫 LLM 重排（現役；最準也最貴，2.5 s 超時＋60 s 快取）
#:   write ＝ 不叫模型：用**寫入期**的 `prior`（提煉模型的 confidence）＋ `utility`（真的被用過幾次）
#:            做標量加權。讀路只剩點乘，毫秒級。
#:   off   ＝ 只照混合分數（語意／字面／時間），不重排也不加權。
RERANK_MODE = str(os.environ.get("BLUEAGENT_RERANK_MODE") or "read").strip().lower()
#: 寫入期 prior 的權重（0–1 的分數，乘上去最多 +W；實測定版前先小一點）
W_PRIOR = float(os.environ.get("BLUEAGENT_W_PRIOR") or 0.05)
#: 效用權重：`usefulness-0.5` 落在 −0.5~+0.5 ⇒ 最多 ±W/2
W_UTILITY = float(os.environ.get("BLUEAGENT_W_UTILITY") or 0.06)


def _routing_on() -> bool:
    """分流開關（與 `app/stages/recall.py` 同一個環境變數；0＝回到舊行為）。"""
    return str(os.environ.get("BLUEAGENT_STREAM_ROUTING") or "1").strip().lower() \
        not in {"0", "off", "false", "no"}


#: 分流後生活流這一側的池子上限。對話流維持 600（舊值＝整個池子）。
#: 為什麼要分：600 條實際只覆蓋 ~2.5 天，而其中 98% 是她自己的活動流水，
#: ⇒ 對話事實兩三天就被擠出候選池（owner 2026-11-03 實測）。
LIFE_POOL_CAP = int(os.environ.get("BLUEAGENT_LIFE_POOL_CAP") or 300)



class MemorySearch:
    def __init__(self, *, memory, raw_log=None, db_path=None, host: str = "owner",
                 vectors=None, reranker=None) -> None:
        #: LLM 重排（`runtime/memory/rerank.py`）：向量粗篩後由它定生死；None＝不重排
        self.reranker = reranker
        #: 向量倉（`runtime/memory/vector_store.py`）；沒有就純字面（行為回到舊版）
        self.vectors = vectors
        self.memory = memory
        self.raw_log = raw_log
        self.path = str(db_path or memory_db_path(memory))
        self.host = host
        #: 窗口摘要的唯一讀寫入口（表結構與 SQL 都在那裡）
        self.windows = WindowStore(self.path)
        #: 關係強度（β 項）：`StanceService`；None＝β 項為 0（舊行為）
        self.stance = None

    # ── 各層 ────────────────────────────────────────────────────────────────
    def _facts(self, entity: str, present: set[str], query: str, limit: int,
               *, qvec: list[float] | None = None,
               include_expired: bool = False, as_of: float | None = None,
               stream: str = "", game: int | None = None) -> list[dict]:
        """事實層：**先取候選池再混合排序**。

        不能只拿 `recall_detail(limit=4)`——那支已經用字面排序截斷，語意才對得上的條目
        根本不在池子裡（「ラノベ ↔ 小説」就是这样被吃掉的）。
        """
        # 被取代的舊值：明確時間點（as_of）⇒ 下面用 `valid_at` 篩成那一刻的版本；
        # 籠統的「以前」⇒ 只跟著同話題（slot）的現值一起出來（`past_topic.py`，防「船長」復活）。
        vague_past = bool(include_expired) and as_of is None
        kw = {"include_expired": include_expired, "include_superseded": as_of is not None or vague_past}
        if stream:
            pool = self._fact_pool(entity, present, stream=stream, **kw)
        elif _routing_on():
            # 兩條流各自取池再合併：對話流不被生活流水擠掉
            pool = (self._fact_pool(entity, present, stream="dialogue", **kw)
                    + self._fact_pool(entity, present, cap=LIFE_POOL_CAP, stream="life", **kw))
        else:
            pool = self._fact_pool(entity, present, **kw)
        if as_of is not None:
            # as-of：只留「那個時間點成立」的版本（含已被取代的舊值）
            pool = [fact for fact in pool if fact.valid_at(float(as_of))]
        if game is not None:
            # 場景維度（2026-11-11 B4）：現實回合只看現實的記憶、桌上回合只看桌的
            pool = [fact for fact in pool if int(getattr(fact, "game", 0) or 0) == int(game)]
        # 注意力場（提案，owner 2026-11-16）：按焦點開窗把「和這個人有關」的關係層記憶拉進候選，
        # 底池只是入口之一；可見性一律由 `_readable` 無條件再過一次（硬約束）。
        focus = attention_field.focus_of(present, entity) if attention_field.enabled() else set()
        if focus:
            _mem = getattr(self.memory, "local", self.memory)
            pool = attention_field.extend(self.path, entity, focus, pool,
                                          to_fact=getattr(_mem, "_row_to_fact", None),
                                          readable=lambda fact: _readable(fact, present),
                                          game=game)
        if not pool:
            return []
        pool = attention_field.drop_fiction(pool)   # 作品不當經歷（2026-11-16）
        newest_focus = attention_field.newest_focus_at(pool, focus) if focus else 0.0
        strength = attention_field.stance_strength(self.stance, entity, focus) if focus else 0.0
        qvec = qvec if qvec is not None else self._query_vector(query)
        self._preload_vectors("fact", entity, [(fact.text, fact.text) for fact in pool],
                              allow_embed=False)
        scored: list[tuple[float, dict]] = []
        for fact in pool:
            at = float(getattr(fact, "created_at", 0) or 0)
            score, parts = self._hybrid(query=query, text=fact.text, at=at, kind="fact",
                                        qvec=qvec, layer=str(getattr(fact, "layer", "") or ""))
            if focus:                                   # 注意力調制：只乘分數，不動可見性
                mult, gain_parts = attention_field.gain(fact, focus, newest_at=newest_focus,
                                                        stance_strength=strength)
                score *= mult
                parts = {**parts, **gain_parts}
            if RERANK_MODE == "write":
                # 寫入期的判斷代替線上重排：把握度高的事實往前，被撈出來卻從沒用過的往後
                score += (W_PRIOR * float(getattr(fact, "prior", 0.0) or 0.0)
                          + W_UTILITY * (float(fact.usefulness) - 0.5))
                parts = {**parts, "prior": round(float(getattr(fact, "prior", 0.0) or 0.0), 3),
                         "usefulness": round(float(fact.usefulness), 3)}
            scored.append((score, {"text": fact.text, "layer": fact.layer, "at": at,
                                   "kind": "fact", "score": round(score, 4), "parts": parts}))
        scored = _gate(scored, floor=self._floor())
        if vague_past:
            scored = scope_superseded(scored, {fact.text: fact for fact in pool})
        return [item[1] for item in scored[:limit]]

    def _fact_pool(self, entity: str, present: set[str], *, cap: int = 600,
                   include_expired: bool = False, stream: str = "",
                   include_superseded: bool = False) -> list:
        """把這個角色（含通用）的事實全部撈出來，用**權威欄位**過披露規則。"""
        mem = getattr(self.memory, "local", self.memory)
        to_fact = getattr(mem, "_row_to_fact", None)
        if to_fact is None or not self.path:
            return []
        try:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            if stream:
                # `stream=''` 是分流前寫的舊資料 ⇒ 當成 dialogue（與 `stream_for_source` 的預設一致）
                rows = db.execute(
                    "SELECT * FROM facts WHERE (entity = ? OR entity = '') "
                    "AND (stream = ? OR (stream = '' AND ? = 'dialogue')) "
                    "ORDER BY created_at DESC LIMIT ?",
                    (str(entity), str(stream), str(stream), cap)).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM facts WHERE entity = ? OR entity = '' "
                    "ORDER BY created_at DESC LIMIT ?", (str(entity), cap)).fetchall()
            db.close()
        except sqlite3.Error:
            return []
        out = []
        for row in rows:
            try:
                fact = to_fact(row)
            except Exception:                                  # noqa: BLE001
                continue
            try:
                # **被取代的預設不進池**（owner 2026-11-03 實測：`include_expired` 連 superseded 也放進來，
                # 舊的「船長」稱呼因此復活）。要查歷史走 `as_of`（呼叫端傳 include_superseded，再按 `valid_at` 篩）。
                if not include_superseded and str(getattr(fact, "superseded_by", "") or ""):
                    continue
                if not include_expired and not fact.is_active():
                    continue
                if not _readable(fact, present):
                    continue
            except Exception:                                  # noqa: BLE001
                continue
            out.append(fact)
        return out

    def _time_intent(self, query: str) -> tuple[float | None, bool]:
        """回 `(as_of, wants_past)`：見 `time_intent.py`（含具體日期）。"""
        return time_intent(query)

    def _floor(self) -> float:
        """有 rerank（讀路）或有寫入期 prior（write 模式）⇒ 粗篩放寬，靠後面的標量砍；
        兩者都沒有 ⇒ 嚴格門檻。"""
        loose = (self.reranker is not None and RERANK_MODE == "read") or RERANK_MODE == "write"
        return CANDIDATE_FLOOR if loose else SEMANTIC_FLOOR

    def _query_vector(self, query: str) -> list[float]:
        if self.vectors is None or self.vectors.embedder is None:
            return []
        rows = self.vectors.embedder.embed([query], kind="query")
        return rows[0] if rows else []

    def _hybrid(self, *, query: str, text: str, at: float, kind: str,
                qvec: list[float], layer: str = "",
                semantic_override: float | None = None) -> tuple[float, dict]:
        """語意 × 0.60 ＋ 字面 × 0.25 ＋ 時間 × 0.15（＋層加權）。回 `(score, parts)`。"""
        lexical = 0.0
        wanted = grams(query)
        if wanted:
            lexical = len(wanted.intersection(grams(text))) / max(1, len(wanted))
        semantic = 0.0
        if semantic_override is not None:
            # 窗口／原文的向量是以 window_id／turn key 存的，不是文本本身 ⇒ 由呼叫端直接給分
            semantic = max(0.0, float(semantic_override))
        elif qvec:
            stored = self._vector_of(kind, text)
            if stored:
                semantic = max(0.0, cosine(qvec, stored))
        recency = self._decay(at)
        boost = LAYER_BOOST.get(layer, 0.0)
        score = (W_SEMANTIC * semantic + W_LEXICAL * lexical
                 + W_RECENCY * recency + boost)
        return score, {"semantic": round(semantic, 3), "lexical": round(lexical, 3),
                       "recency": round(recency, 3), "layer": layer}

    def _vector_of(self, kind: str, ref: str) -> list[float]:
        """從向量倉拿這一條的向量（`ref` 就是文本本身或 window_id／turn key）。"""
        if self.vectors is None:
            return []
        cache = getattr(self, "_vec_cache", None)
        if cache is None:
            cache = {}
            self._vec_cache = cache
        key = (kind, ref)
        if key in cache:
            return cache[key]
        return []

    def _preload_vectors(self, kind: str, entity: str, items: list[tuple[str, str]],
                         *, allow_embed: bool = False) -> dict:
        """把這一層的向量載回（`ref → vector`）。

        `allow_embed=False`（預設）＝**查詢路徑**：只讀已建好的，缺的不補。
        `allow_embed=True`＝暖機／背景索引：可以現場嵌入。
        """
        if self.vectors is None:
            return {}
        cache_key = f"_vectors_{kind}_{entity}"
        table = getattr(self, cache_key, None)
        if table is not None:
            return table
        if allow_embed:
            self.vectors.sync(kind, entity, items)
        table = {ref: vec for ref, vec, _norm in self.vectors.cached(kind, entity)}
        setattr(self, cache_key, table)
        self._vec_cache = {**getattr(self, "_vec_cache", {}),
                           **{(kind, ref): vec for ref, vec in table.items()}}
        return table

    def _decay(self, at: float, *, now: float | None = None) -> float:
        """時間衰減：半衰期 `WINDOW_HALF_LIFE_DAYS` 天，回 0.2–1.0（不會歸零）。"""
        stamp = float(now if now is not None else time.time())
        age_days = max(0.0, (stamp - float(at or 0.0)) / 86400.0)
        factor = 0.5 ** (age_days / WINDOW_HALF_LIFE_DAYS)
        return 0.2 + 0.8 * factor

    def _windows(self, entity: str, present: set[str], query: str, limit: int,
                 *, qvec: list[float] | None = None, game: int | None = None) -> list[dict]:
        rows = self.windows.recent(entity, limit=200, game=game)
        rooms = self._thread_rooms(entity)
        allowed = [row for row in rows
                   if _disclosable(present, _window_room(rooms, row))]
        if not allowed:
            return []
        qvec = qvec if qvec is not None else self._query_vector(query)
        if self.vectors is not None:
            self.vectors.sync("window", entity,
                              [(row["window_id"], f"{row['topic']} {row['summary']}")
                               for row in allowed], limit=0)
        window_vectors = (self.vectors.cached_only("window", entity)
                          if self.vectors is not None else {})
        scored: list[tuple[float, dict]] = []
        for row in allowed:
            body = f"{row['topic']} {row['summary']}"
            vec = window_vectors.get(str(row["window_id"]))
            score, parts = self._hybrid(
                query=query, text=body, at=float(row["ended_at"] or 0), kind="window",
                qvec=qvec,
                semantic_override=(cosine(qvec, vec) if (qvec and vec) else None))
            scored.append((score, {"kind": "window", "topic": row["topic"],
                                   "summary": row["summary"], "at": row["ended_at"],
                                   "turns": row["turns"], "window_id": row["window_id"],
                                   # 門牌：有了它，「這段時間講了什麼」才有得核對
                                   "ptr": str(_row(row, "ptr") or ""),
                                   "score": round(score, 4), "parts": parts,
                                   "open_loops": _json_list(row["open_loops"])}))
        scored = _gate(scored, floor=self._floor())
        return [item[1] for item in scored[:limit]]

    def _turns(self, entity: str, present: set[str], query: str, limit: int,
               *, since: float | None = None, until: float | None = None,
               qvec: list[float] | None = None, game: int | None = None) -> list[dict]:
        """原文層：**用向量索引當候選池**，不是只看最近 120 輪。

        舊版只掃 recent(120) ⇒ 兩天前那句「佐々木とピーちゃんという小説」根本不在候選裡
        （實測：查「小説」回 0，而原文倉裡明明有 10 條）。現在：
          ① 語意：拿已建好的 turn 向量算餘弦取前 `TURN_SEMANTIC_TOP` 條
          ② 字面：對全部原文做 bigram 取前 `TURN_LEXICAL_TOP` 條
          兩路合併後再過披露規則與門檻。
        """
        if self.raw_log is None:
            return []
        rows = self._raw_index(entity)
        if not rows:
            return []
        if since or until:
            rows = [row for row in rows
                    if not (since and float(row.get("at") or 0) < float(since))
                    and not (until and float(row.get("at") or 0) > float(until))]
        rows = [row for row in rows if _disclosable(present, row.get("room"))
                # 場景維度：現實回合翻不到桌上的原文、桌上翻不到現實的（候選生成之前就濾）
                and (game is None or game_of(row.get("source"), row.get("purpose")) == int(game))]
        if not rows:
            return []
        selected: dict[str, dict] = {}
        # ① 語意路（只讀已建好的向量）
        qvec = qvec if qvec is not None else self._query_vector(query)
        cached = self.vectors.cached_only("turn", entity) if self.vectors is not None else {}
        if qvec and cached:
            scored = [(cosine(qvec, vec), key) for key, vec in cached.items()]
            scored.sort(reverse=True)
            by_key = {_turn_key(row): row for row in rows}
            for _score, key in scored[:TURN_SEMANTIC_TOP]:
                row = by_key.get(key)
                if row is not None:
                    selected[key] = row
        # ② 字面路（掃全部原文，取重疊最高的幾條）
        wanted = grams(query)
        if wanted:
            lexical = []
            for row in rows:
                text = str(row.get("text") or "")
                if not text:
                    continue
                overlap = len(wanted.intersection(grams(text))) / max(1, len(wanted))
                if overlap > 0:
                    lexical.append((overlap, row))
            lexical.sort(key=lambda item: (item[0], float(item[1].get("at") or 0)), reverse=True)
            for _overlap, row in lexical[:TURN_LEXICAL_TOP]:
                selected[_turn_key(row)] = row
        if not selected:
            return []
        self._vec_cache = {**getattr(self, "_vec_cache", {}),
                           **{("turn", key): vec for key, vec in cached.items()}}
        out: list[tuple[float, dict]] = []
        for key, row in selected.items():
            text = str(row.get("text") or "")
            vec = cached.get(key) if cached else None
            score, parts = self._hybrid(
                query=query, text=text, at=float(row.get("at") or 0), kind="turn",
                qvec=qvec,
                semantic_override=(cosine(qvec, vec) if (qvec and vec) else None))
            out.append((score, {"kind": "turn", "at": row.get("at"),
                                "speaker": row.get("speaker"), "role": row.get("role"),
                                "text": text, "thread": row.get("thread"),
                                "turn": row.get("turn"), "score": round(score, 4),
                                # 單輪門牌：她可以據此「打開那一句」
                                "ptr": pointers.make(entity, [row.get("turn")]),
                                "parts": parts}))
        out = _gate(out, floor=self._floor())
        return [item[1] for item in out[:limit]]

    def _raw_index(self, entity: str) -> list[dict]:
        """這個角色全部原文（快取 60 秒；查詢層要用它當字面候選池）。"""
        now = time.time()
        cache = getattr(self, "_raw_cache", None)
        if cache and cache[0] == entity and (now - cache[1]) < 60:
            return cache[2]
        rows = self.raw_log.recent(entity, limit=20000)
        self._raw_cache = (entity, now, rows)
        return rows

    def _thread_rooms(self, entity: str) -> dict[str, list[tuple[float, frozenset]]]:
        """thread → [(at, 房間成員)]：窗口摘要沒有自己的房間，要回原文去找。"""
        rooms: dict[str, list[tuple[float, frozenset]]] = {}
        if self.raw_log is None:
            return rooms
        for row in self._raw_index(entity):
            rooms.setdefault(str(row.get("thread") or ""), []).append(
                (float(row.get("at") or 0), frozenset(str(who) for who in (row.get("room") or ()))))
        return rooms

    def carry_window(self, key, started_at: float, ended_at: float) -> str:
        """封片時的承接段：這段時間裡**同一條線**（任何世代）的窗口摘要。

        只看時間重疊不夠（2026-10-24 前就是這樣）：客人那條線封片時，
        同時段主人 1:1 的窗口也重疊 ⇒ 主人的摘要被當成客人那條線的 `[[before]]`。
        """
        line = str(getattr(key, "base", "") or "")
        if not line:
            return ""
        rows = self.windows.overlapping(str(getattr(key, "entity", "") or ""),
                                        started_at, ended_at, limit=20)
        row = next((item for item in rows
                    if str(item["thread"] or "").rsplit("-", 1)[0] == line), None)
        if row is None:
            return ""
        topic = str(row["topic"] or "").strip()
        body = str(row["summary"] or "").strip()
        return (f"{topic}：{body}" if topic else body)[:1500]

    def latest_window(self, entity: str, present, *, game: int | None = 0) -> str:
        """最近一條**在這幾個人面前說得出口**的窗口摘要，前面帶「多久之前」（DIAGNOSIS 2026-11-10 A2）。"""
        people = {str(who).strip() for who in (present or ()) if str(who).strip()}
        people.add(str(entity or ""))
        # `game=0`（預設）：心跳／主動消息要的是**現實**那段，桌那段屬於遊戲（2026-11-11 B4）
        rows = self.windows.recent(str(entity), limit=50, game=game)
        rooms = self._thread_rooms(str(entity))
        for row in rows:
            if not _disclosable(people, _window_room(rooms, row)):
                continue
            topic, body = str(row["topic"] or "").strip(), str(row["summary"] or "").strip()
            text, age = (f"{topic}：{body}" if topic else body), window_age(row)
            return f"（{age}の会話）{text}" if age and text else text   # A2：摘要的「今夜」不是現在
        return ""

    def warmup(self, entities, *, limit: int = 600) -> dict:
        """開機暖機：把 facts／windows／turns 的向量先算好（背景執行緒呼叫）。"""
        if self.vectors is None:
            return {"ok": False, "reason": "no_vectors"}
        detail = {}
        for entity in entities:
            # 分流（owner 2026-11-03）之後**兩條流都要暖**：以前只暖「最新 N 條混池」，
            # 實際上 98% 是她自己的活動流水 ⇒ 對話事實從來沒有向量，語意檢索對它們等於瞎的。
            seen: set[str] = set()
            items: list[tuple[str, str]] = []
            for stream, cap in (("dialogue", limit), ("life", min(int(limit), 200))):
                for fact in self._fact_pool(entity, {str(entity)}, cap=cap, stream=stream):
                    if fact.text in seen:
                        continue
                    seen.add(fact.text)
                    items.append((fact.text, fact.text))
            detail[f"{entity}:fact"] = self.vectors.sync("fact", entity, items)
            detail[f"{entity}:window"] = self.vectors.sync(
                "window", entity, self._window_items(entity))
            if self.raw_log is not None:
                rows = self.raw_log.recent(entity, limit=min(int(limit), 400))
                detail[f"{entity}:turn"] = self.vectors.sync(
                    "turn", entity,
                    [(_turn_key(row), str(row.get("text") or "")) for row in rows])
        return {"ok": True, "synced": detail, "counts": self.vectors.counts()}

    def _window_items(self, entity: str) -> list[tuple[str, str]]:
        return self.windows.items(entity)

    # ── 生活流的主題索引（owner 2026-11-03）──────────────────────────────────
    def life_topics(self, entity: str, *, limit: int = 8) -> list[dict]:
        """她自己的生活按**主題**聚成一條線（實作在 `life_index.py`）。"""
        return life_topics(self.path, entity, limit=limit)

    # ── 一次查完 ────────────────────────────────────────────────────────────
    def search(self, *, entity: str, present, query: str, limit: int = 5,
               since: float | None = None, until: float | None = None,
               layers=("fact", "window", "turn"), stream: str = "",
               game: int | None = None) -> dict:
        text = str(query or "").strip()
        people = {str(who).strip() for who in (present or ()) if str(who).strip()}
        people.add(str(entity or ""))
        if not text:
            return {"ok": False, "error": "empty_query", "facts": [], "windows": [], "turns": []}
        out: dict = {"ok": True, "query": text, "present": sorted(people),
                     "facts": [], "windows": [], "turns": []}
        started = time.monotonic()
        # 查詢向量只算一次（三層共用）
        qvec = self._query_vector(text)
        as_of, wants_past = self._time_intent(text)
        if "fact" in layers:
            out["facts"] = self._facts(str(entity), people, text, limit, qvec=qvec,
                                       include_expired=wants_past, as_of=as_of, stream=stream,
                                       game=game)
        out["time_intent"] = {"as_of": as_of, "past": wants_past}
        if "window" in layers:
            out["windows"] = self._windows(str(entity), people, text, limit, qvec=qvec,
                                           game=game)
        if "turn" in layers:
            # 預算：前面兩層就把時間用完 ⇒ 原文層這輪不查（寧可少回，也不要卡住她回話）
            if (time.monotonic() - started) <= SEARCH_BUDGET_S:
                out["turns"] = self._turns(str(entity), people, text, limit,
                                           since=since, until=until, qvec=qvec, game=game)
            else:
                out["turns_skipped"] = "budget"
        if "topic" in layers:
            # 生活流的主題線（她的 4 萬條碎片按 preset key 聚成幾條線）
            out["topics"] = self.life_topics(str(entity), limit=limit)
        out["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        # LLM 重排：**三層合成一次呼叫**（原本每層一次 ⇒ 一句話打三次 DeepSeek，實測 11 s）
        if self.reranker is not None and RERANK_MODE == "read":
            pool: list[dict] = []
            for layer in ("facts", "windows", "turns"):
                for row in out[layer]:
                    text = (f"{row['topic']} {row['summary']}" if layer == "windows"
                            else str(row.get("text") or ""))
                    pool.append({"text": text, "_layer": layer, "_row": row})
            if pool:
                kept = self.reranker.rerank(text, pool, limit=max(3, limit * 2))
                chosen = {id(item["_row"]) for item in kept}
                for layer in ("facts", "windows", "turns"):
                    out[layer] = [row for row in out[layer] if id(row) in chosen][:limit]
        out["hits"] = len(out["facts"]) + len(out["windows"]) + len(out["turns"])
        return out

    # ── 門：按門牌回原文（實作在 runtime/memory/memory_open.py）────────────────────
    def open(self, *, entity: str, present, pointer: str, game: int | None = None) -> dict:
        """`memory.open`：按門牌把**逐字原文**取回來（見 `memory_open.open_pointer`）。"""
        return open_pointer(getattr(self.raw_log, "root", None), entity=entity,
                            present=present, pointer=pointer, game=game)


def _row(row, key: str, default=""):
    """安全取欄位：舊庫（還沒補 `ptr` 欄位）不會因為 KeyError 讓整個檢索炸掉。"""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _json_list(raw) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _gate(scored: list[tuple[float, dict]], *, floor: float = None) -> list[tuple[float, dict]]:
    """過濾「什麼都回一點」：語意要過絕對門檻，且不能跟最高分差太多；字面命中則放行。"""
    if not scored:
        return []
    best = max(float(item[1].get("parts", {}).get("semantic") or 0.0) for item in scored)
    keep = []
    for score, row in scored:
        parts = row.get("parts") or {}
        semantic = float(parts.get("semantic") or 0.0)
        lexical = float(parts.get("lexical") or 0.0)
        limit_floor = SEMANTIC_FLOOR if floor is None else float(floor)
        if lexical > 0:
            keep.append((score, row))
        elif semantic >= limit_floor and semantic >= (best - SEMANTIC_MARGIN):
            keep.append((score, row))
    keep.sort(key=lambda item: (item[0], float(item[1].get("at") or 0)), reverse=True)
    return keep


def _turn_key(row: dict) -> str:
    return f"{row.get('thread') or ''}#{row.get('turn') or row.get('at')}"


def _scope(entity: str, present: set[str]):
    """Recall 用的 scope：principal 取「她以外的那個人」（沒有就用主人）。"""
    from agent.brain.domain.thread import MemoryScope
    others = sorted(who for who in present if who != entity)
    return MemoryScope(principal=(others[0] if others else "owner"), entity=entity,
                       scene="chat", thread_id="memory.search")
