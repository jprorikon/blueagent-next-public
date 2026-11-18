"""提煉時給模型看「已經記得、而且跟這段對話有關的事」（PENDING B.Agent.3：多值事實的同義碎片，2026-11-17）。

問題：好惡這類**多值、不帶槽位**的事實，同一件事會被換句話說、換語言再寫一次
（實例：「主人怕冷，空调一般开在 26 度」存了 6 條）。單值槽位收不到它們；寫入時的二字重疊 ≥0.8
只擋得住同語言、措辭幾乎一樣的那種，跨語言改寫一個都擋不住。

做法（**交給模型判斷，不做確定性合併**）：每個窗口提煉前，挑出這個角色現有、仍有效、同場景、
不帶槽位的事實裡**跟這段對話最相關**的幾條，放進提煉 prompt：
  「這些已經記得——同一件事別換個說法／換個語言再寫；內容真的變了才寫新的，並在 supersedes 寫原文」。
為什麼不在寫入時按向量相似度直接丟：反義句的向量往往很近（「寒がり」vs「暑がり」），
本機也沒有真實語料能定門檻；丟錯了就是悄悄少一條新事實。模型看得懂「改寫」與「改變」的差別。

相關度：有向量倉（`VectorStore`，事實的 ref 就是正文）⇒ 窗口正文嵌一次、跟已建好的事實向量算餘弦；
沒有向量（或這條還沒建向量）⇒ 退回二字重疊。只讀已建好的事實向量，不現場補嵌事實。
"""

from __future__ import annotations

import os

from agent.brain.memory.facts import grams
from agent.brain.runtime.memory.vector_store import cosine

KNOWN_HINT_LIMIT = int(os.environ.get("BLUEAGENT_DISTILL_KNOWN_HINT") or 12)
#: 餘弦低於這個就不算「有關」（只是挑給模型看，不決定去留，所以寬一點）
COSINE_FLOOR = 0.45
#: 沒有向量時：事實的二字有這個比例出現在窗口裡才算有關
GRAM_FLOOR = 0.3
#: 窗口正文最多拿這麼長去嵌入（太長的窗口只看開頭與結尾）
QUERY_CHARS = 1500


def _query_text(window: str) -> str:
    text = str(window or "")
    if len(text) <= QUERY_CHARS:
        return text
    half = QUERY_CHARS // 2
    return text[:half] + "\n" + text[-half:]


def related_known(memory, entity: str, game: int, window: str, *, vectors=None,
                  limit: int = KNOWN_HINT_LIMIT) -> list[str]:
    """回現有事實裡跟這個窗口最相關的幾條正文（相關度高的在前）。拿不到就回空清單。"""
    if limit <= 0:
        return []
    try:
        local = getattr(memory, "local", None) or memory
        rows = local.snapshot(None, entity=entity).get("facts", []) if hasattr(local, "snapshot") else []
    except Exception:                                       # noqa: BLE001 - 提示拿不到不擋提煉
        return []
    pool = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if (not text or not row.get("active") or str(row.get("slot") or "").strip()
                or int(row.get("game") or 0) != int(game or 0)):
            continue
        pool.append(text)
    pool = list(dict.fromkeys(pool))
    if not pool:
        return []
    qvec: list[float] = []
    cached: dict[str, list[float]] = {}
    if vectors is not None and getattr(vectors, "embedder", None) is not None:
        try:
            embedded = vectors.embedder.embed([_query_text(window)], kind="query")
            qvec = embedded[0] if embedded else []
            cached = vectors.cached_only("fact", entity) if qvec else {}
        except Exception:                                   # noqa: BLE001 - 嵌入服務掛了退回字面
            qvec, cached = [], {}
    window_grams = grams(window)
    scored: list[tuple[float, str]] = []
    for text in pool:
        vec = cached.get(text)
        if qvec and vec:
            score = cosine(qvec, vec)
            if score >= COSINE_FLOOR:
                scored.append((score, text))
            continue
        mine = grams(text)
        if mine and window_grams:
            ratio = len(mine & window_grams) / len(mine)
            if ratio >= GRAM_FLOOR:
                scored.append((ratio, text))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [text for _score, text in scored[:limit]]


def known_hint(texts) -> str:
    """把相關的既有事實寫成提煉 prompt 的一段（插在「会話ウィンドウ：」前，與槽位提示同一個位置）。"""
    rows = [str(text).strip() for text in texts or () if str(text).strip()]
    if not rows:
        return ""
    return ("すでに覚えていること（この会話に関係のありそうなもの）。**同じ内容なら、言い方や言語を変えて"
            "書き直さない**（候補に入れない）。内容が本当に変わったときだけ新しく書き、"
            "supersedes に下の文を一字一句写す：\n"
            + "\n".join(f"  - {row}" for row in rows) + "\n\n")


__all__ = ["KNOWN_HINT_LIMIT", "known_hint", "related_known"]
