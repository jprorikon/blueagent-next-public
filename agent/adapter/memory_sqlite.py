"""記憶的索引後端：SQLite（FTS5 trigram）＋ JSONL 原文。

為什麼要有這個：記憶體版（`memory/in_memory.py`）是「全部事實載進記憶體、用 2-gram 掃一遍」。10k 條以內還行，
再往上就是每輪 O(n) 掃全表。這個後端把檢索交給 FTS5（trigram 對中文有效，不像預設的
unicode61 那樣把中文整段當一個 token），**但原文照樣 append 到 `facts.jsonl`**——
索引壞了可以重建，原文不能丟。

與記憶體版**共用同一套規則**（`memory/fact_rules.py` 抽事實、`Fact.visible_to` 判可見域），
所以換後端不會換行為：同一份資料、同一句 query，召回結果必須逐條一致（有 golden 測試守著）。

不變量（與設計書 §6 相同）：
  · 可見域：`在場的人 ⊆ audience` 才可讀；`self_owned` 永遠可讀；`audience=None` ＝世界
  · 關係/立場不參與判斷（那份在 stance，不在這裡）
  · 檢索只是投影，摘要不影響原文
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
import time
from typing import Iterable, Mapping, Sequence

from agent.adapter import fact_audience_index
from agent.adapter.memory_sqlite_schema import (
    SCHEMA as _SCHEMA,
    migrate_temporal,
    norm_unique_is_global,
)
from agent.brain.memory.fact_rules import facts_for_turn
from agent.brain.memory.fact_store import FactStore
from agent.brain.memory.facts import (LAYER_ORDER, STREAM_DIALOGUE, Fact, fact_key, grams,
                                         join_keys, normalize_text, parse_keys,
                                         register_characters, single_valued, stream_for_source)
from agent.brain.memory.supersede import (adopt_inbound, as_key, link_outbound, retire_slot,
                                             revive, settle_slot, supersede)
from agent.brain.domain.thread import MemoryScope, TurnRecord

#: 命中加權：越靠近具體關係的事實，命中時優先級略高（與記憶體版同一組數字）。
LAYER_BOOST = {"world": 0.0, "consensus": 0.06, "circle": 0.10, "pair": 0.12,
               "private": 0.08}
#: 先取多少個候選再按可見域過濾（FTS 候選池；10k 條規模下足夠）。
CANDIDATE_POOL = 200
def shingles(text: str) -> str:
    """正規化後的二字詞，空白隔開（給 FTS 當 token 用）。"""
    norm = normalize_text(text)
    if len(norm) < 2:
        return norm
    return " ".join(norm[i:i + 2] for i in range(len(norm) - 1))


def _match_query(query: str) -> str:
    """查詢側切同樣的 bigram，做 OR（任一個重疊就算候選）。"""
    parts = [part for part in shingles(query).split(" ") if part]
    return " OR ".join(f'"{part}"' for part in parts)


class SqliteMemory:
    """`MemoryPort` 的索引實作；介面與 `InMemoryMemory` 相同（可直接換）。"""

    def __init__(self, path, *, store: FactStore | None = None,
                 names: Mapping[str, str] | None = None,
                 social=None, seed=None, host: str = "owner",
                 entity_ids: Iterable[str] = (), entity_names: Mapping[str, str] | None = None) -> None:
        """`seed`：空庫時呼叫一次的播種函式（生產不給；demo 種子在 `tests/demo_people.py`）。

        `entity_names`：角色實體 id → 顯示名（`aster` → 「アスター」）。跨倉鏡像進來的那一份
        用它在想起時標「聽誰說的」——`names` 那一份是給**人**的（主人／客人），不含角色。
        """
        self.host = str(host or "")
        #: 認得的角色實體 id（分倉＋跨角色鏡像寫入用）
        self._entity_ids = {str(item).strip() for item in entity_ids if str(item).strip()}
        register_characters(self._entity_ids)       # 角色不算客人（A.13 私記降權）
        self._entity_names = {str(k): str(v) for k, v in (entity_names or {}).items()}
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.store = store
        self._names = names if names is not None else {}   # 活視圖，不複製
        self.social = social
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        # 舊庫沒有 entity 欄（分倉鍵）⇒ 就地補上，值留空（＝遷移前的資料，不屬於任何倉）
        columns = {row["name"] for row in
                   self._db.execute("PRAGMA table_info(facts)").fetchall()}
        if "entity" not in columns:
            self._db.execute(
                "ALTER TABLE facts ADD COLUMN entity TEXT NOT NULL DEFAULT ''")
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS facts_entity ON facts(entity, created_at)")
        self._db.commit()
        self._migrate_temporal()
        fact_audience_index.ensure_ready(self._db)   # 受眾索引「完整」標記（審核 R2）
        #: 舊庫的 `norm` 是全域唯一 ⇒ 跨角色鏡像寫不進第二個倉。只標記、不自動改表
        #: （309MB 的庫要停機重建；遷移工具：`tools/migrations/split_norm_unique.py`）。
        self.norm_unique_is_global = self._norm_unique_is_global()
        if self.norm_unique_is_global:
            print("[memory] facts.norm 還是全域唯一：跨角色鏡像寫不進另一個倉 "
                  "（跑 tools/migrations/split_norm_unique.py 遷移）", flush=True)

        if self.count() == 0:
            self._import_from_store()
        if callable(seed) and self.count() == 0:
            seed(self)

    # ── 基本 ────────────────────────────────────────────────────────────────
    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT count(*) FROM facts").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── 讀 ──────────────────────────────────────────────────────────────────
    def _room(self, scope: MemoryScope, participants: Iterable[str]) -> set[str]:
        people = {str(person).strip() for person in participants if str(person).strip()}
        if scope.principal:
            people.add(scope.principal)
        if scope.entity:
            people.add(str(scope.entity).strip())   # 同上：她是自己房間的固定成員
        return people

    def supersede_slot(self, *, entity: str, slot: str, new_key: str,
                       at: float | None = None, game: int = 0) -> int:
        """同槽位、**同場景**（`game`）寫新值時，把舊現值收掉（valid_to=now、superseded_by=新門牌）。

        回傳收掉的筆數。舊資料**不刪**——「以前是什麼」還能查（as-of）。
        `new_key` 收原文或門牌都行（見 `memory/supersede.py::as_key`）。
        """
        stamp = float(at if at is not None else time.time())   # 記錄與重放要用同一個時刻
        with self._lock:
            changed = retire_slot(self._db, entity=entity, slot=slot, new_key=new_key, at=stamp,
                                  game=int(game or 0))
            if changed:
                self._record_op("retire_slot", entity=str(entity or ""), slot=str(slot),
                                by=as_key(new_key), at=stamp, game=int(game or 0))
            return changed

    def supersede(self, *, entity: str, keys: Iterable[str], new_key: str,
                  at: float | None = None) -> int:
        """把 `keys` 指到的幾條標成「已被 `new_key` 取代」（不刪）。

        `add(..., supersedes=[…])` 走的就是這一支：新的那條記下它取代了誰，
        被取代的那幾條記下被誰取代 ⇒ **兩側各一個哈希指針**。
        """
        stamp = float(at if at is not None else time.time())
        wanted = [as_key(item) for item in (keys or ())]
        with self._lock:
            changed = supersede(self._db, entity=entity, keys=wanted, new_key=new_key, at=stamp)
            if changed:
                self._record_op("supersede", entity=str(entity or ""), keys=wanted,
                                by=as_key(new_key), at=stamp)
            return changed

    def revive(self, key: str) -> int:
        """撤掉「`key` 取代了誰」：被它取代的那幾條回到現值（刪掉替代者就復活）。"""
        with self._lock:
            changed = revive(self._db, key=key)
            if changed:
                self._record_op("revive", key=as_key(key))
            return changed

    def _record_op(self, op: str, **fields) -> None:
        """取代／退場／復活改了既有行 ⇒ 正本**追加**一條更新記錄（返工 R1：不改寫既有行）。

        重放期間不記（重放本身就是在讀這些記錄）。呼叫端持 `self._lock`，
        所以記錄在檔案裡的先後與索引上實際發生的先後一致。
        """
        if self.store is not None and not getattr(self, "_importing", False):
            self.store.append_op(op, **fields)

    def _replay_op(self, record: dict) -> None:
        """重建時重放一條更新記錄（直接動索引，不再記一次）。"""
        op = str(record.get("op") or "")
        with self._lock:
            if op == "retire_slot":
                retire_slot(self._db, entity=str(record.get("entity") or ""),
                            slot=str(record.get("slot") or ""), new_key=str(record.get("by") or ""),
                            at=float(record.get("at") or 0) or None,
                            game=int(record.get("game") or 0))
            elif op == "supersede":
                supersede(self._db, entity=str(record.get("entity") or ""),
                          keys=list(record.get("keys") or ()), new_key=str(record.get("by") or ""),
                          at=float(record.get("at") or 0) or None)
            elif op == "revive":
                revive(self._db, key=str(record.get("key") or ""))

    def note_usage(self, texts: Iterable[str], *, used: bool,
                   at: float | None = None, entity: str | None = None,
                   game: int | None = None) -> int:
        """記帳：這些事實這一輪「被用進回覆」（used=True）還是「撈出來沒用」。

        owner 2026-10-24：對抗「高頻＝有意」的誤判。次數多的口頭禪不該因為次數多就被
        一直撈出來——要看的是 `usefulness = utility / (utility + misses)`。

        `entity`／`game`（2026-11-16 審查返修）：同一句話在三個倉、桌與現實各有一條；
        只記**這個角色、這個場景**那條（A.9「各自學習」）。不給＝舊行為（全倉同文字都記）。
        """
        stamp = float(at if at is not None else time.time())
        normed = [normalize_text(str(text)) for text in (texts or [])]
        normed = [item for item in normed if item]
        if not normed:
            return 0
        column = "utility" if used else "misses"
        scope, scope_params = "", []
        if entity:
            scope += " AND entity = ?"
            scope_params.append(str(entity))
        if game is not None:
            scope += " AND COALESCE(game, 0) = ?"
            scope_params.append(int(game))
        changed = 0
        with self._lock:
            for norm in normed:
                cursor = self._db.execute(
                    f"UPDATE facts SET {column} = {column} + 1"
                    + (", last_used_at = ?" if used else "")
                    + " WHERE norm = ?" + scope,
                    ((stamp, norm) if used else (norm,)) + tuple(scope_params))
                changed += int(cursor.rowcount or 0)
            self._db.commit()
        return changed

    def _norm_unique_is_global(self) -> bool:
        """這張表是不是還把 `norm` 當成全域唯一（舊 schema）。"""
        return norm_unique_is_global(self._db)

    def _migrate_temporal(self) -> None:
        """舊庫補時域欄位（SQLite 沒有 IF NOT EXISTS 加欄，只能查 pragma）。"""
        with self._lock:
            migrate_temporal(self._db)

    def _row_to_fact(self, row: sqlite3.Row) -> Fact:
        audience = row["audience"]
        return Fact(
            text=str(row["text"]), layer=str(row["layer"]),
            audience=None if audience is None else frozenset(
                part for part in str(audience).split(",") if part),
            label=str(row["label"]), principal=str(row["principal"]),
            entity=str(row["entity"] or ""),
            consensus=bool(row["consensus"]), self_owned=bool(row["self_owned"]),
            source=str(row["source"]), created_at=float(row["created_at"]),
            kind=str(row["kind"] or "") if "kind" in row.keys() else "",
            valid_from=float(row["valid_from"] or 0) if "valid_from" in row.keys() else 0.0,
            valid_to=float(row["valid_to"] or 0) if "valid_to" in row.keys() else 0.0,
            superseded_by=str(row["superseded_by"] or "") if "superseded_by" in row.keys() else "",
            supersedes=parse_keys(row["supersedes"]) if "supersedes" in row.keys() else (),
            key=str(row["key"] or "") if "key" in row.keys() else "",
            slot=str(row["slot"] or "") if "slot" in row.keys() else "",
            utility=int(row["utility"] or 0) if "utility" in row.keys() else 0,
            misses=int(row["misses"] or 0) if "misses" in row.keys() else 0,
            last_used_at=float(row["last_used_at"] or 0) if "last_used_at" in row.keys() else 0.0,
            fiction=int(row["fiction"] or 0) if "fiction" in row.keys() else 0,
            prior=float(row["prior"] or 0) if "prior" in row.keys() else 0.0,
            stream=str(row["stream"] or "") if "stream" in row.keys() else "",
            topic=str(row["topic"] or "") if "topic" in row.keys() else "",
            game=int(row["game"] or 0) if "game" in row.keys() else 0,
        )

    def _candidates(self, query: str, entity: str | None = None) -> list[sqlite3.Row]:
        """FTS5 取候選：查詢切成 bigram 做 OR，任一個重疊都進池（再由呼叫端算重疊率）。"""
        match = _match_query(query)
        if not match:
            return []
        with self._lock:
            try:
                if entity is None:
                    return self._db.execute(
                        "SELECT f.*, bm25(facts_fts) AS rank FROM facts_fts "
                        "JOIN facts f ON f.id = facts_fts.rowid "
                        "WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
                        (match, CANDIDATE_POOL)).fetchall()
                return self._db.execute(
                    "SELECT f.*, bm25(facts_fts) AS rank FROM facts_fts "
                    "JOIN facts f ON f.id = facts_fts.rowid "
                    "WHERE facts_fts MATCH ? AND (f.entity = ? OR f.entity = '') "
                    "ORDER BY rank LIMIT ?",
                    (match, entity, CANDIDATE_POOL)).fetchall()
            except sqlite3.OperationalError:
                return []

    def _hearsay(self, scope: MemoryScope, fact: Fact) -> Fact:
        """跨倉鏡像（GLOSSARY.md「鏡像」）進來的那一條 ⇒ 標成「聽誰說的」（GLOSSARY.md「轉述」）。

        鏡像寫入時 `principal` 填的是**當初聽她說的那個角色**（`aster`／`nori`），
        這裡換成顯示名。她提起時就要用轉述口吻，不能說成對方親口告訴她的。
        認不得的 principal（人／舊資料）一律不動：那不是轉述，是她自己那一份。
        """
        who = str(fact.principal or "").strip()
        if not who or who == str(scope.entity or "").strip() or who not in self._entity_ids:
            return fact
        return fact.with_hearsay(self._entity_names.get(who, who))

    def recall_detail(self, scope: MemoryScope, query: str, *, limit: int = 4,
                      participants: Iterable[str] = (), stream: str = "",
                      game: int | None = None) -> list[Fact]:
        people = self._room(scope, participants)
        rows = self._candidates(query, str(scope.entity or "") or None)
        q = grams(query)
        scored: list[tuple[float, Fact]] = []
        for row in rows:
            fact = self._row_to_fact(row)
            if not fact.is_active():
                # 事件過期／被新值取代 ⇒ 不進被動注入（但原文仍在，可由 memory.search 指定查歷史）
                continue
            # 場景維度（2026-11-11 B4）：現實回合只看現實、桌上回合只看桌上
            if game is not None and int(fact.game or 0) != int(game):
                continue
            # 她自己那層永遠可讀（owner 2026-11-03 線路 A），其餘走披露閘
            if not fact.readable(people):
                continue
            # 分流（owner 2026-11-03）：指定了流就只看那一條；`stream=''` 的舊資料視同 dialogue
            if stream and (fact.stream or STREAM_DIALOGUE) != stream:
                continue
            # 與記憶體版同一條公式：重疊率 + 層加權（不靠 bm25，避免兩邊排序漂）
            overlap = len(q & grams(fact.text)) / max(1, len(q)) if q else 0.0
            scored.append((overlap + LAYER_BOOST.get(fact.layer, 0.0), fact))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        # owner 2026-10-23：沒有命中就回空（理由同記憶體版：底噪＝無關內容）
        return [self._hearsay(scope, fact) for _, fact in scored[:limit]]

    def recall(self, scope: MemoryScope, query: str, *, limit: int = 4,
               participants: Iterable[str] = (), stream: str = "",
               game: int | None = None) -> Sequence[str]:
        return [fact.recall_text for fact in self.recall_detail(
            scope, query, limit=limit, participants=participants, stream=stream, game=game)]

    def probe(self, scope: MemoryScope, query: str, *, participants=()) -> int:
        people = self._room(scope, participants)
        q = grams(query)
        if not q:
            return 0
        hits = 0
        for row in self._candidates(query, str(scope.entity or "") or None):
            fact = self._row_to_fact(row)
            if fact.readable(people) and (q & grams(fact.text)):
                hits += 1
        return hits

    # ── 寫 ──────────────────────────────────────────────────────────────────
    def record(self, scope: MemoryScope, turn: TurnRecord, *,
               participants: Iterable[str] = ()) -> None:
        facts = facts_for_turn(scope, turn, participants=participants,
                               names=self._names)
        for fact in facts:
            self.add(fact.text, layer=fact.layer,
                     audience=None if fact.audience is None else set(fact.audience),
                     label=fact.label, principal=fact.principal,
                     consensus=fact.consensus, self_owned=fact.self_owned,
                     source=fact.source, entity=fact.entity, game=fact.game)
        # 她 ↔ 另一個角色 agent ⇒ 雙方各按自己的角度落一份（owner 2026-10-23）
        me = str(scope.entity or "")
        for other in sorted({str(w).strip() for w in (participants or ())
                              if str(w).strip() in self._entity_ids
                              and str(w).strip() != me}):
            for fact in facts:
                if str(fact.layer) == "private":
                    # 秘密只留在說出口的那一倉：鏡像出去等於替她把秘密告訴第三個人
                    continue
                self.add(fact.text, layer=fact.layer,
                         # 她當時在場 ⇒ 房間成員要含她（否則那條對她永遠是「不可說」）
                         audience=(None if fact.audience is None
                                   else set(fact.audience) | {other}),
                         label=fact.label, principal=me, entity=other,
                         consensus=fact.consensus, self_owned=False,
                         source=str(fact.source) + "+mirror", game=fact.game)

    def add(self, text: str, *, layer: str = "private", audience=None,
            label: str = "", principal: str = "", consensus: bool = False,
            self_owned: bool = False, source: str = "manual",
            entity: str = "", kind: str = "", valid_from: float = 0.0,
            valid_to: float = 0.0, superseded_by: str = "", slot: str = "",
            supersedes: Iterable[str] = (), prior: float = 0.0,
            stream: str = "", topic: str = "", game: int = 0, fiction: int = 0,
            created_at: float | None = None, key: str = "") -> Fact:
        """寫一條事實。`supersedes` 是「它取代了哪幾條」（門牌或原文都可以）。

        寫進來之後做兩件事（`_link_supersedes`）：
          ① 被它取代的那幾條 `superseded_by = 它的門牌`；
          ② 如果先呼叫了 `supersede_slot`／`supersede`（舊的那一側已經指過來），
             把那些門牌補進它的 `supersedes` ⇒ 兩側一定有指針，不管呼叫順序。
        """
        stamp = float(created_at if created_at is not None else time.time())
        fact = Fact(text=str(text).strip(), layer=layer,
                    audience=None if audience is None else frozenset(audience),
                    label=label, principal=principal, entity=str(entity or ""),
                    consensus=consensus, self_owned=self_owned, source=source,
                    created_at=stamp,
                    kind=str(kind or ""), valid_from=float(valid_from or 0),
                    valid_to=float(valid_to or 0), superseded_by=str(superseded_by or ""),
                    supersedes=tuple(str(item).strip() for item in (supersedes or ())
                                     if str(item).strip()),
                    slot=str(slot or ""), prior=float(prior or 0),
                    # 流：沒指定就按來源推導（`memory/facts.stream_for_source`）
                    stream=str(stream or "") or stream_for_source(source),
                    topic=str(topic or ""), game=int(game or 0), fiction=int(fiction or 0))
        if not fact.text:
            return fact
        norm = normalize_text(fact.text)
        if not norm:
            return fact
        fact.key = str(key or "").strip() or fact_key(fact.text)
        with self._lock:
            exists = self._db.execute("SELECT 1 FROM facts WHERE norm = ? AND entity = ?",
                                      (norm, fact.entity)).fetchone()
            if exists is not None:
                return fact
            if single_valued(fact.kind, fact.slot) and not fact.superseded_by:
                # 單值槽位只留一條現值，按事件時間定（owner 2026-11-16）；較舊的晚到 ⇒ 一落庫就是「已被取代」
                by, by_at = settle_slot(self._db, entity=fact.entity, slot=fact.slot, key=fact.key,
                                        at=fact.valid_from or fact.created_at, game=fact.game)
                if by:
                    fact.superseded_by = by
                    fact.valid_to = by_at if not fact.valid_to or fact.valid_to > by_at else fact.valid_to
            try:
                cursor = self._db.execute(
                    "INSERT INTO facts (text, norm, layer, audience, label, principal, "
                    "entity, consensus, self_owned, source, created_at, "
                    "kind, valid_from, valid_to, superseded_by, supersedes, key, slot, prior, "
                    "shingles, stream, topic, game, fiction) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fact.text, norm, fact.layer,
                     None if fact.audience is None else ",".join(sorted(fact.audience)),
                     fact.label, fact.principal, fact.entity, int(fact.consensus),
                     int(fact.self_owned), fact.source, fact.created_at,
                     fact.kind, fact.valid_from, fact.valid_to, fact.superseded_by,
                     join_keys(fact.supersedes), fact.key, fact.slot,
                     float(fact.prior or 0), shingles(fact.text),
                     fact.stream, fact.topic, int(fact.game or 0), int(fact.fiction or 0)))
                # 受眾索引（owner 2026-11-16）：焦點窗靠它走索引，不再 LIKE 掃全表
                fact_audience_index.index_fact(self._db, int(cursor.lastrowid or 0), fact)
                self._db.commit()
            except sqlite3.IntegrityError:
                # 舊庫還是「norm 全域唯一」⇒ 同一句話落不進第二個倉（跨角色鏡像會撞）。
                # 這裡不拋（一拋就整輪記憶寫入失敗），改回已存在的那一條並要求遷移。
                self._db.rollback()
                self.norm_unique_is_global = True
                row = self._db.execute("SELECT * FROM facts WHERE norm = ?", (norm,)).fetchone()
                return self._row_to_fact(row) if row is not None else fact
            # 同一把鎖裡寫索引、補指針、追加正本 ⇒ 正本裡的先後與索引上實際發生的先後一致
            self._link_supersedes(fact)
            if self.store is not None and not getattr(self, "_importing", False):
                # 原文優先：索引只是投影。這一行帶著 `supersedes`，重放時 `_link_supersedes`
                # 會把舊行的指針原樣補回來 ⇒ 這裡不必（也不准）改寫正本裡的舊行。
                self.store.append(fact)
        return fact

    def _link_supersedes(self, fact: Fact) -> int:
        """把取代關係補成**雙向**（舊→新的 inbound、新→舊的 outbound）。回傳被改動的舊行數。

        舊行的失效時間取**新事實的生效時間**（`valid_from`，沒有才用 `created_at`），
        不用「現在」（2026-11-15 WP3）：重建時要重放這一步，用「現在」的話每重建一次
        被取代者的 `valid_to` 就漂一次；而且「新值從何時成立」本來就是舊值失效的時刻。
        """
        if not fact.key:
            return 0
        mutated = 0
        with self._lock:
            if fact.supersedes:
                mutated += supersede(self._db, entity=fact.entity, keys=fact.supersedes,
                                     new_key=fact.key,
                                     at=fact.valid_from or fact.created_at,
                                     game=fact.game)
            link_outbound(self._db, entity=fact.entity, key=fact.key, by=fact.superseded_by)
            merged = adopt_inbound(self._db, entity=fact.entity, key=fact.key,
                                   supersedes=fact.supersedes, game=fact.game)
            if merged != tuple(fact.supersedes):
                fact.supersedes = merged
                self._db.execute("UPDATE facts SET supersedes = ? WHERE key = ? AND entity = ?",
                                 (join_keys(merged), fact.key, fact.entity))
                self._db.commit()
                mutated += 1
        return mutated

    def fact_by_text(self, text: str, entity: str | None = None) -> Fact | None:
        """正規化後比對，回本地那一條（含 layer/audience 等權威欄位）。"""
        norm = normalize_text(text)
        if not norm:
            return None
        with self._lock:
            if entity:
                row = self._db.execute(
                    "SELECT * FROM facts WHERE norm = ? AND (entity = ? OR entity = '')",
                    (norm, str(entity))).fetchone()
            else:
                row = self._db.execute("SELECT * FROM facts WHERE norm = ?",
                                       (norm,)).fetchone()
        return self._row_to_fact(row) if row is not None else None

    def find_similar(self, text: str, *, threshold: float = 0.5,
                     entity: str | None = None) -> Fact | None:
        """找本地最像的那一條（Hindsight 會把原句改寫，不能只靠全等）。

        為什麼要容忍改寫：Hindsight 的抽取層會把「主人喜欢薄荷（probe）。」沉成
        「主人喜欢薄荷。」——用全等比對會全部對不上，於是所有語義命中都被丟掉。
        回 `None` 表示「本地沒有足夠像的」⇒ 呼叫端必須丟棄（ACL 不能靠猜）。
        """
        q = grams(text)
        if not q:
            return None
        best: tuple[float, Fact] | None = None
        for row in self._candidates(text, str(entity) if entity else None):
            fact = self._row_to_fact(row)
            overlap = len(q & grams(fact.text)) / max(1, len(q))
            if best is None or overlap > best[0]:
                best = (overlap, fact)
        if best is None or best[0] < threshold:
            return None
        return best[1]

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM facts")
            self._db.commit()
        if self.store is not None:
            self.store.clear()

    # ── 維護 ────────────────────────────────────────────────────────────────
    def _import_from_store(self) -> int:
        """DB 空、但 jsonl 有東西 ⇒ 從原文重建索引（索引可以丟，原文不行）。"""
        if self.store is None:
            return 0
        imported = 0
        self._importing = True
        try:
            # 事實行與更新記錄按檔案順序重放（取代／退場／復活是追加記錄，見 fact_store.py）
            for fact in self.store.load_records():
                if isinstance(fact, dict):
                    self._replay_op(fact)
                    continue
                before = self.count()
                self.add(fact.text, layer=fact.layer,
                         audience=None if fact.audience is None else set(fact.audience),
                         entity=fact.entity, label=fact.label, principal=fact.principal,
                         consensus=fact.consensus, self_owned=fact.self_owned,
                         source=fact.source, created_at=fact.created_at,
                         kind=fact.kind, valid_from=fact.valid_from,
                         valid_to=fact.valid_to, superseded_by=fact.superseded_by,
                         slot=fact.slot, prior=fact.prior, key=fact.key,
                         # 原文優先：重建索引時取代關係與場景／流／主題要跟著回來（否則指針全斷、桌變現實）
                         supersedes=fact.supersedes, stream=fact.stream,
                         topic=fact.topic, game=fact.game)
                if self.count() > before:
                    imported += 1
        finally:
            self._importing = False
        return imported

    def rebuild(self) -> int:
        """刪掉索引、只從 jsonl 重建（等價於 `rm facts.db` 再開）。"""
        with self._lock:
            self._db.execute("DELETE FROM facts")
            self._db.commit()
        return self._import_from_store()

    # ── UI ──────────────────────────────────────────────────────────────────
    def snapshot(self, participants: Iterable[str] | None = None,
                 entity: str = "") -> dict:
        people = None if participants is None else set(participants)
        if people is not None and entity:
            people.add(str(entity).strip())   # 披露層的「在場」含她自己
        with self._lock:
            if entity:
                rows = self._db.execute(
                    "SELECT * FROM facts WHERE entity = ? OR entity = '' ORDER BY id",
                    (str(entity),)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM facts ORDER BY id").fetchall()
        facts = [self._row_to_fact(row).to_dict(participants=people)
                 for row in rows]
        counts: dict[str, int] = {}
        hidden: dict[str, int] = {}
        for fact in facts:
            bucket = counts if fact["visible"] else hidden
            bucket[fact["layer"]] = bucket.get(fact["layer"], 0) + 1
        return {"facts": facts, "visible_counts": counts, "hidden_counts": hidden,
                "total": len(facts), "room": sorted(people or ()),
                # 「已被取代」的條數：記憶頁要能一眼看出有幾條是舊的（不刪，可以走回去）
                "superseded": sum(1 for fact in facts if not fact["active"]),
                "backend": "sqlite"}

    def health(self) -> dict:
        with self._lock:
            size = self.path.stat().st_size if self.path.exists() else 0
        return {"backend": "sqlite", "path": str(self.path), "facts": self.count(),
                "bytes": size}


__all__ = ["CANDIDATE_POOL", "LAYER_BOOST", "SqliteMemory"]
