"""心情的「主要原因」，以及**主人看不看得見**它（情感整理 P4，docs/PLAN-affect-reorg-20261109.md §3-2）。

AEM 只給一個點（valence／arousal），看不出「為什麼」；`AffectService.timeline()` 把每一筆 episode
（種類、對象、增量、什麼時候失效）攤開。這裡從**還有效的** episode 裡挑 |Δv| 最大的那件當主要原因，
再按對象判它主人看不看得見：

  · 看得見：對象是主人、而且是主人那一句話引起的（感興趣、被拒絕、被打斷）；
  · 看不見：她自己的活動（對象是她自己／活動）、公共區的反饋（`board:`）、
    等回音（`ignored`）、主人不在（核心規則的 `concern`）。

只讀、不改狀態；拿不到就回空 dict（呼叫端照舊）。
"""

from __future__ import annotations

import time

#: 原因 → 給模型的一句人話（不給數字、不給術語）
_WORDS = {
    "self_down": "自分の作業がうまくいかなかった",
    "self_up": "自分の作業が進んだ",
    "board_up": "投稿に反応があった",
    "waiting": "前に送った一言に返事がまだない",
    "absence": "しばらくオーナーと話せていない",
    "owner": "オーナーとのやりとり",
}


def _target_kind(row: dict) -> str:
    """一筆 episode → 原因鍵。對象 id 的約定：主人＝`owner`、等回音＝`waiting`、公共區＝`board:<帖>`、
    她自己的活動＝她的 id 或活動 id。"""
    target = str(row.get("target") or "")
    kind = str(row.get("kind") or "")
    up = float(row.get("delta_valence") or 0.0) >= 0
    if target == "waiting":
        return "waiting"
    if target.startswith("board"):
        return "board_up" if up else "self_down"
    if target in {"owner", ""}:
        # 核心的 absence 規則對象也記成主人，但主人不知道她怎麼想 ⇒ 看不見
        return "absence" if kind == "concern" else "owner"
    return "self_up" if up else "self_down"


def cause_key(row: dict) -> str:
    """一筆 episode → 原因鍵（給管理台列表用；`main_cause()` 內部也用這支）。"""
    return _target_kind(dict(row or {}))


def cause_words(key: str) -> str:
    """原因鍵 → 給人看的一句（沒有就空字串）。"""
    return _WORDS.get(str(key or ""), "")


def is_hidden(key: str) -> bool:
    """這個原因，主人看不看得見（看得見只有「主人那一句引起的」）。"""
    return str(key or "") != "owner"


def annotate(affect, entity: str, *, limit: int = 20, since: float | None = None) -> list[dict]:
    """`timeline()` ＋ 每筆的「主要原因看不看得見」（管理台的列表用；不寫檔）。"""
    rows = affect.timeline(entity, limit=limit, since=since) or []
    for row in rows:
        key = cause_key(row)
        row.update(cause=key, words=cause_words(key), hidden=is_hidden(key),
                   down=float(row.get("delta_valence") or 0.0) < 0)
    return rows


def main_cause(affect, entity: str, *, now: float | None = None, limit: int = 12) -> dict:
    """`{"key", "words", "hidden", "down"}`；沒有有效的 episode 就回空 dict。"""
    timeline = getattr(affect, "timeline", None)
    if timeline is None:
        return {}
    try:
        rows = list(timeline(str(entity), limit=limit) or [])
    except Exception:                                     # noqa: BLE001 - 讀不到就當不知道
        return {}
    at = float(now if now is not None else time.time())
    live = [row for row in rows
            if not float(row.get("expires_at") or 0.0) or float(row["expires_at"]) >= at]
    if not live:
        return {}
    top = max(live, key=lambda row: abs(float(row.get("delta_valence") or 0.0)))
    key = _target_kind(top)
    return {"key": key, "words": _WORDS.get(key, ""), "hidden": key != "owner",
            "down": float(top.get("delta_valence") or 0.0) < 0}


__all__ = ["cause_key", "cause_words", "is_hidden", "main_cause"]
