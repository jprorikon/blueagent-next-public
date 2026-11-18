"""睡着的時間流速（owner 2026-11-16「睡眠中衰减率应该有所…」）。

AEM 的 v／a 是**讀時投影**：每個 episode 的值按 `2^(-elapsed/half_life)` 衰減，`elapsed` 就是
「從那次事件到現在過了多久」。這支給它一個換算函式：**睡眠時段的一秒只算 0.5 秒**
（`BLUEAGENT_AEM_SLEEP_DECAY` 可調；`1` ＝ 舊行為、`0` ＝ 睡着期間完全凍結）。

為什麼是「放慢」而不是「加速」：睡着時沒有新刺激，心情本來就該慢慢平復；如果夜裡曲線還在
明顯走動（或掉得比白天快），看起來就像她半夜還在情緒起伏。放慢之後，睡前那一刻的情緒會撐過
整夜、早上起來才繼續走——與「睡一覺起來心情差不多」的直覺一致。

⚠️ 只影響**衰減的計時**，不影響：新事件的評分、`expires_at`、預約／注意力掃描、
以及沒有生物鐘的場合（`time_scale=None` ⇒ 與舊行為逐位元相同）。
"""

from __future__ import annotations

import os

#: 睡眠時段的時間流速（1 秒睡著的時間＝這麼多秒的有效衰減時間）
DEFAULT_SCALE = 0.5


def scale_from_env() -> float:
    raw = os.environ.get("BLUEAGENT_AEM_SLEEP_DECAY")
    if raw is None or not str(raw).strip():
        return DEFAULT_SCALE
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return DEFAULT_SCALE


def overlap_seconds(t0: float, t1: float, spans) -> float:
    """`[t0, t1]` 與睡窗重疊幾秒。"""
    if t1 <= t0:
        return 0.0
    total = 0.0
    for start, end in spans or ():
        low, high = max(float(t0), float(start)), min(float(t1), float(end))
        if high > low:
            total += high - low
    return total


def effective_seconds(t0: float, t1: float, spans, scale: float) -> float:
    """把 `[t0, t1]` 換成「有效衰減秒數」：睡著那段乘 `scale`。"""
    overlap = overlap_seconds(t0, t1, spans)
    scale = max(0.0, min(1.0, float(scale)))
    return (float(t1) - float(t0)) - overlap + overlap * scale


def provider(spans_for, scale: float | None = None):
    """`spans_for() -> [(start, end)]` ⇒ `time_scale(t0, t1) -> 有效秒數`。"""
    weight = scale_from_env() if scale is None else float(scale)
    cache: dict = {}

    def time_scale(t0: float, t1: float) -> float:
        key = "spans"
        if key not in cache:
            try:
                cache[key] = tuple(spans_for() or ())
            except Exception:                          # noqa: BLE001 - 算不出來就照舊
                cache[key] = ()
        return effective_seconds(t0, t1, cache[key], weight)

    return time_scale


def from_clocks(clocks, *, days: int = 14, scale: float | None = None):
    """`{entity: CircadianClock}` ⇒ `entity -> time_scale`（睡窗按需讀、每個 process 快取一次）。"""
    table = dict(clocks or {})
    weight = scale_from_env() if scale is None else float(scale)
    per_entity: dict[str, object] = {}

    def for_entity(entity: str):
        key = str(entity)
        if key not in per_entity:
            clock = table.get(key)
            if clock is None or not hasattr(clock, "sleep_spans"):
                per_entity[key] = lambda t0, t1: float(t1) - float(t0)     # 沒有生物鐘 ⇒ 舊行為
            else:
                per_entity[key] = provider(lambda: clock.sleep_spans(days=days), weight)
        return per_entity[key]

    return for_entity


__all__ = ["DEFAULT_SCALE", "effective_seconds", "from_clocks", "overlap_seconds", "provider",
           "scale_from_env"]
