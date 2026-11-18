"""檢索重排（rerank）：向量只管「像不像」，最後由 LLM 判「到底是不是同一件事」。

為什麼要有這層（實測）：jina-clip-v2 對「ラノベ ↔ 小説」能給 0.9，但邊緣案例會落在
0.55–0.62 之間——門檻調低會放進雜訊（「おはよう」0.59），調高又漏掉真的。單靠一個閾值
永遠是取捨；交給 LLM 逐條判「這句話跟問題是不是同一件事」就沒有這個取捨。

設計原則：
  · **只判不改寫**：回的是索引編號，不是新句子（避免模型編造記憶）
  · **候選要短**：一次最多 8 條、每條截 160 字（成本與延遲都可控）
  · **失敗就放行**（fail-open）：LLM 掛了/回傳壞掉 ⇒ 原樣保留，不讓檢索變空
  · **不碰披露規則**：rerank 只看已通過 ACL 的候選
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time

MAX_CANDIDATES = 8
MAX_CHARS = 160

#: 判定指令（**日語**，review 2026-10-28：以前整段繁中）。輸出 JSON 形狀 `{"keep": [...]}` 不變。
PROMPT = """あなたは記憶検索の審査係。以下は一つの問いと、いくつかの記憶の候補（番号 1..N）。
**本当に同じことを話している**候補だけを選ぶ。言い換え（ラノベ＝小説＝軽小説＝novel）は同じこととみなす。
話題が近いだけで別のことを話しているものは選ばない。

JSON だけを出力：{{"keep": [番号...]}}。関係するものが一つもなければ {{"keep": []}}。
説明も前置きも書かない。

問い：{query}

候補：
{items}
"""


def _items(rows: list[dict]) -> str:
    lines = []
    for index, row in enumerate(rows[:MAX_CANDIDATES], start=1):
        text = str(row.get("text") or row.get("summary") or "").replace("\n", " ")
        lines.append(f"{index}. {text[:MAX_CHARS]}")
    return "\n".join(lines)


def parse_keep(raw: str, total: int) -> list[int] | None:
    """解析模型輸出；壞掉回 None（呼叫端 fail-open）。"""
    body = str(raw or "").strip()
    if not body:
        return None
    match = re.search(r"\{.*\}", body, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("keep"), list):
            out = []
            for value in data["keep"]:
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if 1 <= index <= total:
                    out.append(index - 1)
            return sorted(set(out))
    # 退路：模型只吐了數字（"2,3"）
    numbers = [int(num) for num in re.findall(r"\d+", body)]
    keep = [num - 1 for num in numbers if 1 <= num <= total]
    return sorted(set(keep)) if keep else None


class Reranker:
    def __init__(self, call, *, min_candidates: int = 2, timeout_s: float = 2.5,
                 cache_ttl_s: float = 60.0) -> None:
        self.call = call
        self.min_candidates = int(min_candidates)
        #: 超時就放棄（回原排序）——寧可少一次精修，也不要讓她回話卡 5 秒
        self.timeout_s = float(timeout_s)
        #: 同一問題＋同一批候選，一分鐘內不重複問模型
        self.cache_ttl_s = float(cache_ttl_s)
        self.calls = 0
        self.timeouts = 0
        self.hits = 0
        self.dropped = 0
        self.last_error = ""
        self._cache: dict[str, tuple[float, list[int]]] = {}

    def rerank(self, query: str, rows: list[dict], *, limit: int) -> list[dict]:
        if not rows or len(rows) <= max(1, self.min_candidates) or self.call is None:
            return rows[:limit]
        head = rows[:MAX_CANDIDATES]
        prompt = PROMPT.format(query=str(query)[:300], items=_items(head))
        # 快取：同問題＋同候選（60 s 內）直接用上次的判定
        cache_key = hashlib.sha1(
            (str(query)[:300] + "\u0000" + "\u0001".join(str(row.get("text") or "") for row in head)
             ).encode("utf-8")).hexdigest()
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < self.cache_ttl_s:
            self.hits += 1
            return [head[index] for index in cached[1] if index < len(head)][:limit]
        raw = self._call_with_timeout(prompt)
        if raw is None:
            return rows[:limit]
        self.calls += 1
        keep = parse_keep(str(raw or ""), len(head))
        if keep is None:
            self.last_error = "unparsable"
            return rows[:limit]
        self._cache[cache_key] = (time.time(), keep)
        self.dropped += len(head) - len(keep)
        chosen = [head[index] for index in keep]
        # 被砍掉的候選之後的（第 9 條起）本來就沒給模型看，直接丟
        return chosen[:limit]

    def _call_with_timeout(self, prompt: str):
        """把模型呼叫放進執行緒，超過 `timeout_s` 就放棄（回 None ⇒ 走 fail-open）。"""
        box: dict = {}

        def worker() -> None:
            try:
                box["value"] = self.call(prompt)
            except Exception as exc:                          # noqa: BLE001
                box["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(self.timeout_s)
        if thread.is_alive():
            self.timeouts += 1
            self.last_error = f"timeout>{self.timeout_s}s"
            return None
        if "error" in box:
            self.last_error = str(box["error"])
            return None
        return box.get("value")

    def snapshot(self) -> dict:
        return {"calls": self.calls, "timeouts": self.timeouts, "cache_hits": self.hits,
                "dropped": self.dropped, "error": self.last_error}
