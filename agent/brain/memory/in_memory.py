from __future__ import annotations

import re
import threading
import time
from typing import Callable, Iterable, Mapping, Sequence

from agent.brain.memory.fact_rules import facts_for_turn
from agent.brain.memory.facts import (LAYER_ORDER, Fact, fact_key, grams, join_keys,
                                         normalize_text, parse_keys, single_valued,
                                         stream_for_source)
from agent.brain.memory.facts import STREAM_DIALOGUE
from agent.brain.memory.fact_store import FactStore
from agent.brain.domain.social import SocialGraph
from agent.brain.domain.thread import MemoryScope, TurnRecord


class InMemoryMemory:
    def __init__(self, names: Mapping[str, str] | None = None,
                 social: SocialGraph | None = None,
                 store: FactStore | None = None,
                 host: str = "owner",
                 entity_ids: Iterable[str] = (),
                 entity_names: Mapping[str, str] | None = None,
                 seed: Callable[["InMemoryMemory"], None] | None = None) -> None:
        """`seed`：空庫時呼叫一次的播種函式（生產不給；demo 種子在 `tests/demo_people.py`）。

        `names` 不複製：傳 `PrincipalRegistry.names()` 那種活視圖，之後核可的客人也有名字。
        `entity_names`：角色實體 id → 顯示名，跨倉鏡像「聽誰說的」要用（與 SqliteMemory 同名）。
        """
        self._lock = threading.RLock()
        #: 主人（host）的 principal id：他在場時走子集規則；guest 單獨在場只能讀自己的
        self._host = str(host or "")
        self._names = names if names is not None else {}
        self._social = social
        self.store = store
        #: 認得的角色實體 id（分倉＋跨角色鏡像寫入用）
        self._entity_ids = {str(item).strip() for item in entity_ids if str(item).strip()}
        self._entity_names = {str(k): str(v) for k, v in (entity_names or {}).items()}
        self._facts: list[Fact] = list(store.load()) if store is not None else []
        for fact in self._facts:
            fact.key = fact.key or fact_key(fact.text)
        if callable(seed) and not any(fact.source == "seed" for fact in self._facts):
            seed(self)

    # ── 讀 ────────────────────────────────────────────────────────────────────
    def recall(self, scope: MemoryScope, query: str, *, limit: int = 4,
               participants: Iterable[str] = (), stream: str = "",
               game: int | None = None) -> Sequence[str]:
        return [fact.recall_text for fact in self.recall_detail(
            scope, query, limit=limit, participants=participants, stream=stream, game=game)]

    def recall_detail(self, scope: MemoryScope, query: str, *, limit: int = 4,
                      participants: Iterable[str] = (), stream: str = "",
                      game: int | None = None) -> list[Fact]:
        people = self._room(scope, participants)
        with self._lock:
            pool = [fact for fact in self._facts
                    if self._in_bin(fact, scope.entity) and fact.disclosable(people)
                    and fact.is_active()      # 被取代／過期的不進被動注入（與 SqliteMemory 一致）
                    and (not stream or (fact.stream or STREAM_DIALOGUE) == stream)
                    # 場景維度（2026-11-11 B4）：現實回合只看現實、桌上回合只看桌上
                    and (game is None or int(fact.game or 0) == int(game))]
        q = grams(query)
        scored: list[tuple[float, Fact]] = []
        for fact in pool:
            overlap = len(q & grams(fact.text)) / max(1, len(q)) if q else 0.0
            if overlap > 0:
                # 越靠近具體關係的記憶，命中時優先級略高；世界觀事實做底噪。
                boost = {"world": 0.0, "consensus": 0.06, "circle": 0.10,
                         "pair": 0.12, "private": 0.08}.get(fact.layer, 0.0)
                scored.append((overlap + boost, self._hearsay(scope, fact)))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        # owner 2026-10-23：**沒有命中就回空**。以前會塞「可讀的底噪」，
        # 實測等於把 5 條無關事實（還包含 demo 種子）塞進 prompt，讓她順著亂講。
        return [fact for _, fact in scored[:limit]]

    def _hearsay(self, scope: MemoryScope, fact: Fact) -> Fact:
        """跨倉鏡像進來的那一條 ⇒ 標成「聽誰說的」（與 SqliteMemory 同一條規則）。"""
        who = str(fact.principal or "").strip()
        if not who or who == str(scope.entity or "").strip() or who not in self._entity_ids:
            return fact
        return fact.with_hearsay(self._entity_names.get(who, who))

    def probe(self, scope: MemoryScope, query: str, *, participants=()) -> int:
        """便宜探針：只數「可讀且語義有重疊」的事實，不取全文、不跑 LLM。"""
        people = self._room(scope, participants)
        with self._lock:
            pool = [fact for fact in self._facts
                    if self._in_bin(fact, scope.entity) and fact.disclosable(people)]
        q = grams(query)
        if not q:
            return 0
        return sum(1 for fact in pool if q & grams(fact.text))

    # ── 寫 ────────────────────────────────────────────────────────────────────
    def record(self, scope: MemoryScope, turn: TurnRecord, *,
               participants: Iterable[str] = ()) -> None:
        # 規則本身在 `fact_rules.py`（兩個後端共用同一份；這裡只負責落存）
        facts = facts_for_turn(scope, turn, participants=participants,
                               names=self._names)
        for fact in facts:
            self._add_locked(fact)
        # 她 ↔ 另一個角色 agent：**雙方各按自己的角度落一份**（owner 2026-10-23）
        # 對方那一份的 `principal` 是「這一側的 entity」——從他的視角看，當事人是她。
        for other in self._other_entities(scope, participants):
            for fact in facts:
                if str(fact.layer) == "private":
                    continue          # 秘密不鏡像（與 SqliteMemory 同一條）
                self._add_locked(Fact(
                    text=fact.text, layer=fact.layer,
                    audience=(None if fact.audience is None
                              else frozenset(fact.audience) | {other}),
                    label=fact.label, principal=str(scope.entity or ""),
                    entity=str(other), consensus=fact.consensus,
                    self_owned=False, source=str(fact.source) + "+mirror",
                    game=fact.game))          # 鏡像出去的那一份也是同一個場景

    def add(self, text: str, *, layer: str = "private", audience=None,
            label: str = "", principal: str = "", consensus: bool = False,
            self_owned: bool = False, source: str = "manual",
            entity: str = "", kind: str = "", valid_from: float = 0.0,
            valid_to: float = 0.0, superseded_by: str = "", slot: str = "",
            supersedes: Iterable[str] = (), prior: float = 0.0,
            stream: str = "", topic: str = "", game: int = 0, fiction: int = 0) -> Fact:
        # `kind`／`valid_from`／`valid_to`／`slot`／`supersedes` 與 SqliteMemory.add 同一個介面：
        # 少了它們，呼叫端（行動日誌那份 public sink）會 TypeError 而被靜默吞掉
        # （2026-10-24 踩到：事實根本沒進記憶，測試卻是綠的）。
        fact = Fact(text=str(text).strip(), layer=layer,
                    audience=None if audience is None else frozenset(audience),
                    label=label, principal=principal, entity=str(entity or ""),
                    consensus=consensus, self_owned=self_owned, source=source,
                    kind=str(kind or ""), valid_from=float(valid_from or 0),
                    valid_to=float(valid_to or 0), superseded_by=str(superseded_by or ""),
                    supersedes=tuple(str(item).strip() for item in (supersedes or ())
                                     if str(item).strip()),
                    slot=str(slot or ""), prior=float(prior or 0),
                    stream=str(stream or "") or stream_for_source(source),
                    topic=str(topic or ""), game=int(game or 0), fiction=int(fiction or 0))
        fact.key = fact_key(fact.text)
        with self._lock:
            self._settle_slot_locked(fact)
            self._add_locked(fact)
            self._link_supersedes_locked(fact)
        return fact

    def _settle_slot_locked(self, fact: Fact) -> None:
        """單值槽位只留一條現值，按事件時間定（與 `supersede.settle_slot` 同一條規則）。"""
        if not single_valued(fact.kind, fact.slot) or fact.superseded_by or not fact.text:
            return
        norm = normalize_text(fact.text)
        if any(normalize_text(other.text) == norm and str(other.entity or "") == str(fact.entity or "")
               for other in self._facts):
            return                                # 重複的那條根本不會落庫
        happened = lambda item: float(item.valid_from or item.created_at or 0)   # noqa: E731
        stamp = happened(fact)
        rows = [other for other in self._facts
                if str(other.entity or "") == str(fact.entity or "") and other.slot == fact.slot
                and int(other.game or 0) == int(fact.game or 0) and not other.superseded_by
                and other.key != fact.key and (not other.valid_to or other.valid_to > stamp)]
        newer = [other for other in rows if happened(other) > stamp]
        winner = max(newer, key=happened) if newer else None
        by_key, by_at = (winner.key, happened(winner)) if winner is not None else (fact.key, stamp)
        for other in rows:
            if other is winner:
                continue
            other.superseded_by = by_key
            other.valid_to = by_at if not other.valid_to or other.valid_to > by_at else other.valid_to
        if winner is not None:
            fact.superseded_by = by_key
            fact.valid_to = by_at if not fact.valid_to or fact.valid_to > by_at else fact.valid_to
            winner.supersedes = parse_keys(join_keys(
                {*winner.supersedes, fact.key, *(o.key for o in rows if o is not winner)} - {by_key}))

    def supersede_slot(self, *, entity: str, slot: str, new_key: str,
                       at: float | None = None, game: int = 0) -> int:
        """同槽位舊現值收掉（與 SqliteMemory 同名同義）：不刪，只標指針。

        只收**同一個場景**（`game`）的：桌上的狀態不能把現實的現值收掉，反之亦然。
        """
        key = fact_key(new_key) or str(new_key or "")
        stamp = float(at if at is not None else time.time())
        picked = 0
        with self._lock:
            for fact in self._facts:
                if (str(fact.entity or "") != str(entity or "") or str(fact.slot or "") != str(slot or "")
                        or fact.superseded_by or not fact.slot
                        or int(fact.game or 0) != int(game or 0)):
                    continue
                if fact.valid_to and fact.valid_to <= stamp:
                    continue
                fact.valid_to, fact.superseded_by = stamp, key
                picked += 1
        return picked

    def revive(self, key: str) -> int:
        """撤掉「`key` 取代了誰」（刪掉替代者，舊的就復活）。"""
        target = str(key or "")
        revived = 0
        with self._lock:
            for fact in self._facts:
                if target and fact.superseded_by == target:
                    fact.superseded_by, fact.valid_to = "", 0.0
                    revived += 1
            for fact in self._facts:
                if fact.key == target and fact.supersedes:
                    fact.supersedes = ()
        return revived

    def _link_supersedes_locked(self, fact: Fact) -> None:
        """兩側指針補齊（舊→新的 inbound、新→舊的 outbound）。"""
        if not fact.key:
            return
        for old in fact.supersedes:
            for other in self._facts:
                if (other.key == old and str(other.entity or "") == str(fact.entity or "")
                        and int(other.game or 0) == int(fact.game or 0)   # 取代不跨場景
                        and not other.superseded_by):
                    other.superseded_by = fact.key
                    other.valid_to = other.valid_to or fact.created_at
        for other in self._facts:
            # 不變量的最後一道（與 `supersede.adopt_inbound` 同一條）：指錯場景的舊行放回現值
            if (other.superseded_by == fact.key and other.key != fact.key
                    and str(other.entity or "") == str(fact.entity or "")
                    and int(other.game or 0) != int(fact.game or 0)):
                other.superseded_by, other.valid_to = "", 0.0
        inbound = sorted({other.key for other in self._facts
                          if other.superseded_by == fact.key and other.key
                          and other.key != fact.key
                          and str(other.entity or "") == str(fact.entity or "")
                          and int(other.game or 0) == int(fact.game or 0)})
        if inbound:
            fact.supersedes = parse_keys(join_keys((*fact.supersedes, *inbound)))

    # ── UI ────────────────────────────────────────────────────────────────────
    def snapshot(self, participants: Iterable[str] | None = None,
                 entity: str = "") -> dict:
        people = None if participants is None else set(participants)
        if people is not None and entity:
            people.add(str(entity).strip())   # 披露層的「在場」含她自己
        with self._lock:
            facts = [fact.to_dict(participants=people)
                     for fact in self._facts if self._in_bin(fact, entity)]
        counts: dict[str, int] = {}
        hidden: dict[str, int] = {}
        for fact in facts:
            if fact["visible"]:
                counts[fact["layer"]] = counts.get(fact["layer"], 0) + 1
            else:
                hidden[fact["layer"]] = hidden.get(fact["layer"], 0) + 1
        return {"facts": facts, "visible_counts": counts, "hidden_counts": hidden,
                "total": len(facts), "room": sorted(people or ())}

    # ── 內部 ────────────────────────────────────────────────────────────────────
    def _room(self, scope: MemoryScope, participants: Iterable[str]) -> set[str]:
        people = {str(person).strip() for person in participants if str(person).strip()}
        if scope.principal:
            people.add(scope.principal)
        # 她的回合裡她一定在場：披露層用「在場的人 ⊆ 房間成員」判，
        # 少了這一格，她一個人時會對所有記憶都判成「不可說」。
        if scope.entity:
            people.add(str(scope.entity).strip())
        return people

    @staticmethod
    def _in_bin(fact: Fact, entity) -> bool:
        """這條事實是不是「這個角色實體那一倉」的。

        空 entity＝分倉之前的舊資料 ⇒ 對任何倉都放行（遷移腳本會補上真正的 entity）。
        """
        target = str(entity or "").strip()
        owner = str(getattr(fact, "entity", "") or "").strip()
        if not target or not owner:
            return True
        return owner == target

    def _other_entities(self, scope: MemoryScope, participants: Iterable[str]) -> list[str]:
        me = str(scope.entity or "").strip()
        return sorted({str(who).strip() for who in (participants or ())
                       if str(who).strip() in self._entity_ids
                       and str(who).strip() != me})

    def _add_locked(self, fact: Fact) -> None:
        if not fact.text:
            return
        norm = normalize_text(fact.text)
        if any(normalize_text(existing.text) == norm
               and str(existing.entity or "") == str(fact.entity or "")
               for existing in self._facts):
            return
        self._facts.append(fact)
        if self.store is not None:
            self.store.append(fact)

    def clear(self) -> None:
        with self._lock:
            self._facts = []
            if self.store is not None:
                self.store.clear()
