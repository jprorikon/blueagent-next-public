"""心情的「曲線」與「統計」——都只讀，不改狀態（`AffectService.series()`／`stats()` 的實作）。

為什麼獨立一支：`affect.py` 是適配層的心臟（評分、寫入、衰減、語氣），已經頂到 600 行上限
（`test_code_size`）；這兩支是**純讀**的投影與彙總，搬出來讓心臟留在原來的尺寸。

  · `series()`：歷史曲線。valence／arousal 是 episode 的漏積分，所以**任意歷史時刻都能重算**——
    這不是事後補記的取樣，是同一套模型的真值（管理台「心情 AEM」的曲線用它）。
  · `stats()`：近 N 小時的正負比例與平均增量（判斷「是不是只會被推高」）。

兩個都吃 `AffectService` 當第一個參數（拿 `_store().show()` 與 `policy`），不 import 它，避免循環。
"""

from __future__ import annotations

import time

from agent.brain.vendor import aem_v2


def series(affect, entity: str, *, hours: float = 24.0, step: float = 300.0,
           at: float | None = None) -> list[dict]:
    """任意時刻的 valence／arousal（關閉式投影，不寫檔、不改狀態）。

    `step` 越小越平滑也越貴（24h／5 分鐘＝288 個點）。
    """
    if not getattr(affect, "enabled", False):
        return []
    now = float(at if at is not None else time.time())
    span = max(0.5, min(24.0 * 14, float(hours))) * 3600.0
    step = max(30.0, min(3600.0, float(step)))
    try:
        state = affect._store(entity).show()
    except Exception:                                   # noqa: BLE001 - 沒有狀態就回空曲線
        return []
    points: list[dict] = []
    hook = getattr(affect, "time_scale", None)
    time_scale = hook(entity) if callable(hook) else None    # 睡眠時段放慢衰減（owner 2026-11-16）
    moment = now - span
    while moment <= now:
        try:
            view = aem_v2.project(state, moment, time_scale=time_scale)
        except Exception:                               # noqa: BLE001
            view = {}
        row = view.get("affect") or {}
        points.append({"at": round(moment, 1),
                       "valence": round(float(row.get("valence") or 0.0), 4),
                       "arousal": round(float(row.get("arousal") or 0.0), 4)})
        moment += step
    return points


def stats(affect, entity: str, *, hours: float = 24.0) -> dict:
    """近 N 小時的評分統計：正負比例、平均增量。"""
    since = float(affect._clock()) - float(hours) * 3600.0
    rows = affect.timeline(entity, limit=10_000, since=since)
    count = len(rows)
    if not count:
        return {"hours": hours, "count": 0}
    valence = [row["delta_valence"] for row in rows]
    arousal = [row["delta_arousal"] for row in rows]
    return {
        "hours": hours, "count": count,
        "valence_mean": round(sum(valence) / count, 3),
        "valence_min": round(min(valence), 3),
        "valence_max": round(max(valence), 3),
        "negative_ratio": round(sum(1 for value in valence if value < 0) / count, 3),
        "arousal_mean": round(sum(arousal) / count, 3),
        "arousal_negative": sum(1 for value in arousal if value < 0),
        "kinds": {kind: sum(1 for row in rows if row["kind"] == kind)
                  for kind in {row["kind"] for row in rows}},
    }


__all__ = ["series", "stats"]
