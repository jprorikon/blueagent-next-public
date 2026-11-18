"""籠統的「以前」＋話題檢索（owner 2026-11-03）。

問題裡只有「以前／之前／上次」、沒有明確時間點時：被取代的舊值**只跟著同一個話題的現值一起出現**。
話題＝事實的 `slot`（「主人正在讀的書」「主人の呼び方」這種槽位）。

  · 「以前我在讀什麼書？」⇒ 現值《另一本》命中 ⇒ 同槽的舊值《佐々木とピーちゃん》一起帶出來
  · 問別的事 ⇒ 稱呼槽沒有現值命中 ⇒ 舊的「船長」不會因為「以前」兩個字就復活

帶出來的舊值前面標「（以前の話）」，她才不會把它當成現在成立的事。
明確時間點（as_of）不走這裡——那條由 `valid_at` 篩成「那一刻成立的版本」。

從 `memory_search.py` 分出來（那支有 600 行棘輪）。
"""

from __future__ import annotations

#: 舊值的標記（進 prompt ⇒ 日語）
PAST_MARK = "（以前の話）"


def _superseded(fact) -> bool:
    return bool(str(getattr(fact, "superseded_by", "") or ""))


def _slot(fact) -> str:
    return str(getattr(fact, "slot", "") or "").strip()


def scope_superseded(scored: list[tuple[float, dict]], facts_by_text: dict) -> list[tuple[float, dict]]:
    """`scored`（已過門檻、已排序）裡的舊值：同槽有現值命中才留，並加上舊值標記；沒有槽位的舊值一律不留。"""
    topics = {_slot(fact) for _score, row in scored
              for fact in [facts_by_text.get(row.get("text"))]
              if fact is not None and not _superseded(fact) and _slot(fact)}
    out: list[tuple[float, dict]] = []
    for score, row in scored:
        fact = facts_by_text.get(row.get("text"))
        if fact is not None and _superseded(fact):
            if _slot(fact) not in topics:
                continue
            row = {**row, "text": PAST_MARK + str(row.get("text") or ""), "past": True}
        out.append((score, row))
    return out


__all__ = ["PAST_MARK", "scope_superseded"]
