"""窗口摘要的「復讀」判定（owner 2026-11-03；同日從 `distill` 拆出來壓行數）。

實測 nori 的窗口被同一個 guest 用同一句問了 16 次、她回了 60 輪同一句 ⇒ 生成的摘要
全是「她一直重複同一句」，卻占著摘要池。這支負責在提煉前把這種窗口擋掉。
"""

from __future__ import annotations

#: 同一句重複到這個次數 ⇒ 判為復讀／空轉（不提煉）
REPEAT_LIMIT = 4
#: 整窗只有這麼多種不同的話、且條數夠多 ⇒ 沒有資訊可抽
DISTINCT_MIN = 2
TURNS_MIN = 6


def repetition_reason(texts: list[str], *, limit: int = REPEAT_LIMIT) -> str:
    """回「為什麼不值得提煉」：`repetitive`/`no_content`；值得提煉就回空字串。"""
    counts: dict[str, int] = {}
    for text in texts:
        key = "".join(str(text or "").split())
        if key:
            counts[key] = counts.get(key, 0) + 1
    if counts and max(counts.values()) >= max(2, int(limit)):
        return "repetitive"
    if len(counts) <= DISTINCT_MIN and len(texts) >= TURNS_MIN:
        return "no_content"
    return ""


__all__ = ["repetition_reason", "REPEAT_LIMIT", "DISTINCT_MIN", "TURNS_MIN"]
