"""記憶：**和現役 blueagent 同一個框架**（Hindsight bank）＋本地原文/ACL 層。

為什麼是「兩層」而不是二選一：

  · Hindsight 負責**語義召回**（embedding＋評分）——現役用的就是它，庫在 :8897。
  · 但 Hindsight 的 bank 鍵是「端末×角色」，它**不懂**新倉的硬規則
    `在場的人 ⊆ audience`。若把層/可見域交給它判，會先撈出不能說的東西。
  · 所以：**ACL 與原文留在本地 SQLite**（authoritative），Hindsight 只當語意索引。
    召回流程＝Hindsight 取候選 → 回本地按 audience 過濾 → 不足則用本地底噪補。
    過濾後才進 prompt，所以「Hindsight 撈到了不該看的」也不會漏出去。

bank 映射照設計書 §6.3（與現役 `blueagent-<slug>-<角色>` 同一族）：

    shared  → blueagent-<entity>              （world / consensus / circle）
    private → blueagent-<entity>-u-<principal> （private / pair）
    self    → blueagent-<entity>-self          （她自己的经历）

失敗語義：Hindsight 掛了／逾時 ⇒ 整層當沒有，退回本地 SQLite 檢索。
**記憶檢索失敗從不擋對話**（現役橋同一條紀律）。
"""

from __future__ import annotations

import json
import pathlib
import time
import urllib.error
import urllib.request
from typing import Iterable, Mapping, Sequence

from agent.adapter.memory_sqlite import SqliteMemory
from agent.brain.memory.fact_store import FactStore
from agent.brain.memory.facts import LAYER_ORDER, Fact, grams, normalize_text
from agent.brain.domain.thread import MemoryScope, TurnRecord

#: 哪些層住哪個池（與設計書 §6.3 一致）。
BANK_OF_LAYER = {"world": "shared", "consensus": "shared", "circle": "shared",
                 "pair": "private", "private": "private", "self": "self"}


class HindsightMemory:
    """`MemoryPort` 的 Hindsight 實作；本地 SQLite 保原文與 ACL。"""

    def __init__(self, path, *, host: str, prefix: str = "blueagent",
                 store: FactStore | None = None, names: Mapping[str, str] | None = None,
                 social=None, timeout: float = 2.0, top_k: int = 8,
                 context: str = "", seed=None,
                 entity_ids: Iterable[str] = (),
                 entity_names: Mapping[str, str] | None = None) -> None:
        self.local = SqliteMemory(path, store=store, names=names, social=social,
                                  seed=seed, entity_ids=entity_ids,
                                  entity_names=entity_names)
        self.host = str(host or "").rstrip("/")
        self.prefix = str(prefix or "blueagent").strip("-")
        self.timeout = float(timeout)
        self.top_k = int(top_k)
        self.context = str(context or "")
        self.last_error = ""

    # ── bank 映射 ───────────────────────────────────────────────────────────
    def bank_for(self, scope: MemoryScope, layer: str = "") -> str:
        kind = BANK_OF_LAYER.get(layer, "self" if scope.self_owned else "shared")
        base = f"{self.prefix}-{scope.entity}"
        if kind == "private":
            principal = str(scope.principal or "").strip()
            return f"{base}-u-{principal}" if principal else ""
        if kind == "self":
            return f"{base}-self"
        return base

    # ── 寫 ──────────────────────────────────────────────────────────────────
    def add(self, text: str, *, layer: str = "private", audience=None, label: str = "",
            principal: str = "", consensus: bool = False, self_owned: bool = False,
            source: str = "manual", entity: str = "", kind: str = "",
            valid_from: float = 0.0, valid_to: float = 0.0, superseded_by: str = "",
            slot: str = "", supersedes: Iterable[str] = (), prior: float = 0.0,
            stream: str = "", topic: str = "", game: int = 0) -> Fact:
        # 介面與 SqliteMemory.add 對齊：少一個參數，提煉器整批就會 TypeError（靜默丟事實）
        fact = self.local.add(text, layer=layer, audience=audience, label=label,
                              principal=principal, consensus=consensus,
                              self_owned=self_owned, source=source, entity=entity,
                              kind=kind, valid_from=valid_from, valid_to=valid_to,
                              superseded_by=superseded_by, slot=slot,
                              supersedes=supersedes, prior=prior,
                              stream=stream, topic=topic, game=game)
        if fact.text:
            scope = MemoryScope(principal=principal, entity=entity or "mira",
                                scene="", thread_id="", self_owned=self_owned)
            bank = self.bank_for(scope, layer)
            if bank:
                self._push(bank, fact)
        return fact

    def record(self, scope: MemoryScope, turn: TurnRecord, *,
               participants: Iterable[str] = ()) -> None:
        self.local.record(scope, turn, participants=participants)

    def _push(self, bank: str, fact: Fact) -> bool:
        """把一條事實推進 Hindsight（`async=true`，不等抽取完成，不拖慢對話）。"""
        if not self.host:
            return False
        context = self.context or (
            f"layer={fact.layer}; audience={fact.audience_text() or 'world'}")
        body = {"items": [{"content": fact.text, "context": context}], "async": True}
        try:
            self._post(f"/v1/default/banks/{bank}/memories", body, timeout=5.0)
            return True
        except Exception as exc:                       # noqa: BLE001 - 寫不進去不該擋對話
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    # ── 讀 ──────────────────────────────────────────────────────────────────
    def recall_detail(self, scope: MemoryScope, query: str, *, limit: int = 4,
                      participants: Iterable[str] = (), stream: str = "",
                      game: int | None = None) -> list[Fact]:
        people = set(self.local._room(scope, participants))  # noqa: SLF001 - 同一個 ACL 規則
        candidates = self._semantic(scope, query, people)
        if game is not None:                                  # 場景維度（2026-11-11 B4）：語義候選也按 game 過濾
            candidates = [fact for fact in candidates if int(fact.game or 0) == int(game)]
        if stream:                                            # 分流（2026-11-03）：先篩流再排
            candidates = [fact for fact in candidates if (fact.stream or "dialogue") == stream]
        chosen = self._rank(candidates, query, people, limit)
        if len(chosen) < limit:
            seen = {normalize_text(fact.text) for fact in chosen}
            for fact in self.local.recall_detail(scope, query, limit=limit,
                                                 participants=participants, stream=stream,
                                                 game=game):
                if normalize_text(fact.text) in seen:
                    continue
                chosen.append(fact)
                if len(chosen) >= limit:
                    break
        return chosen[:limit]

    def _semantic(self, scope: MemoryScope, query: str, people: set[str]) -> list[Fact]:
        """向 Hindsight 要多個池的候選；**每個候選都回本地取權威事實**（含 ACL 欄位）。"""
        query = str(query or "").strip()
        if not self.host or not query:
            return []
        banks = {self.bank_for(scope, "world"), self.bank_for(scope, "self")}
        if scope.principal:
            banks.add(self.bank_for(scope, "private"))
        banks.discard("")
        facts: list[Fact] = []
        for bank in sorted(banks):
            for row in self._recall_bank(bank, query):
                text = str(row.get("text") or "")
                # Hindsight 會改寫原句（抽取層），所以用「最像的一條」而不是全等；
                # 對不上就丟——ACL 不能靠猜。
                fact = (self.local.fact_by_text(text, entity=str(scope.entity or "") or None)
                        or self.local.find_similar(text, entity=str(scope.entity or "") or None))
                if fact is None:
                    continue
                if fact.disclosable(people):
                    facts.append(fact)
        return facts

    def _recall_bank(self, bank: str, query: str) -> list[dict]:
        body = {"query": query[:500], "budget": "low", "max_tokens": 500}
        try:
            data = self._post(f"/v1/default/banks/{bank}/memories/recall", body,
                              timeout=self.timeout)
        except Exception as exc:                       # noqa: BLE001 - 掛了就當沒有
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        rows = data.get("results") if isinstance(data, dict) else data
        return [row for row in (rows or []) if isinstance(row, dict)][: self.top_k]

    def _rank(self, candidates: list[Fact], query: str, people: set[str],
              limit: int) -> list[Fact]:
        q = grams(query)
        scored: list[tuple[float, Fact]] = []
        seen: set[str] = set()
        for fact in candidates:
            key = normalize_text(fact.text)
            if key in seen or not fact.disclosable(people):
                continue
            seen.add(key)
            overlap = len(q & grams(fact.text)) / max(1, len(q)) if q else 0.0
            scored.append((overlap + {"world": 0.0, "consensus": 0.06, "circle": 0.10,
                                      "pair": 0.12, "private": 0.08}.get(fact.layer, 0.0),
                           fact))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [fact for _, fact in scored[:limit]]

    def recall(self, scope: MemoryScope, query: str, *, limit: int = 4,
               participants: Iterable[str] = (), stream: str = "",
               game: int | None = None) -> Sequence[str]:
        return [fact.text for fact in self.recall_detail(
            scope, query, limit=limit, participants=participants, stream=stream, game=game)]

    def probe(self, scope: MemoryScope, query: str, *, participants=()) -> int:
        return self.local.probe(scope, query, participants=participants)

    # ── 轉發本地 ────────────────────────────────────────────────────────────
    def snapshot(self, participants: Iterable[str] | None = None,
                 entity: str = "") -> dict:
        payload = self.local.snapshot(participants, entity=entity)
        payload["backend"] = "hindsight"
        payload["hindsight"] = self.health()
        return payload

    def clear(self) -> None:
        self.local.clear()

    def close(self) -> None:
        self.local.close()

    def count(self) -> int:
        return self.local.count()

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _post(self, path: str, body: dict, *, timeout: float) -> dict:
        request = urllib.request.Request(
            f"{self.host}{path}", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}

    def health(self) -> dict:
        if not self.host:
            return {"ok": False, "error": "no host"}
        started = time.monotonic()
        try:
            with urllib.request.urlopen(f"{self.host}/health", timeout=3.0) as response:
                alive = response.status == 200
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "host": self.host, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": alive, "host": self.host, "prefix": self.prefix,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "last_error": self.last_error}


__all__ = ["BANK_OF_LAYER", "HindsightMemory"]
