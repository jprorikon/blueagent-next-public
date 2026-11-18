"""自我活動不自旋（owner 2026-11-09「我应该如何防止它们自旋」→ 做 1／2／3／4／6）。

自旋＝產出變成下一輪的輸入、輸入變成下一輪的產出，**沒有任何新東西從外面進來**。
例：aster 的 observe-peers 1018 件、method 432 件；notes-tidy 讀自己的記錄寫新記錄，第二天 notes-reread 再讀它。
以前的閘全是「頻率」（每天幾次、間隔多久），不看「這次有沒有新材料」與「這次跟上次有沒有不同」。

這裡只放規則（純函式，不碰檔案），由 `runtime/life.py` 的 `Rotation.pick` 用：

  ① **輸入門檻**：每套活動標 `inputs`——`external`（讀外面）／`none`（自己創作）／`own`（讀自己的產出）／
     `peers`（讀同伴的產出）／`maintenance`（週期維護，另有 `every_days`）。
     `own`／`peers` 是**閉環**：上次做這套之後，要有**一手材料**進來才排得進去——
       · `own`：主人跟她說過話，或她自己做過一件 `external`／`none` 的事；
       · `peers`：同伴做過一件 `external`／`none` 的事（同伴的閉環產出不算新材料）。
     閉環活動自己的產出**不算**新材料 ⇒ notes-tidy 的產出餵不進 notes-reread。
  ② **跨活動比產出**：收尾時拿這次的產出跟同一套最近 3 次比；太像（二字重疊 ≥0.8）⇒ 這套冷卻
     1 → 2 → 4 天（上限 7 天），直到又寫出不一樣的東西。
  ③ **閉環配額**：一天裡閉環活動最多佔 1/3（算上這一件）。
"""

from __future__ import annotations

EXTERNAL, NONE, OWN, PEERS, MAINTENANCE = "external", "none", "own", "peers", "maintenance"
#: 會把一手材料帶進來的活動（對「自己」與「同伴」都算）
FIRST_ORDER = frozenset({EXTERNAL, NONE})
CLOSED = frozenset({OWN, PEERS})

#: 跟最近幾次比
NOVELTY_WINDOW = 3
#: 二字重疊（佔較短那份）到這裡就算「跟上次一樣」
NOVELTY_OVERLAP = 0.8
#: 冷卻上限（天）
MAX_COOLDOWN_DAYS = 7


def inputs_of(preset) -> str:
    return str(getattr(preset, "inputs", "") or EXTERNAL)


def is_closed(preset) -> bool:
    return inputs_of(preset) in CLOSED


def has_fresh_material(entity: str, preset, *, since: float, picks: dict, kinds: dict,
                       heard_at: float = 0.0) -> bool:
    """閉環那幾套：上次做這套（`since`）之後有沒有一手材料進來。非閉環一律 True。

    `picks`：`{entity: [{"key", "at"}…]}`（輪換史，三個角色都在）；`kinds`：`{entity: {key: inputs}}`。
    沒做過（`since`=0）⇒ True（第一次總要讓她做一次）。
    """
    if not is_closed(preset) or not since:
        return True
    mode = inputs_of(preset)
    if mode == OWN and float(heard_at or 0.0) > since:
        return True
    owners = [str(entity)] if mode == OWN else [name for name in picks if name != str(entity)]
    for who in owners:
        table = kinds.get(who) or {}
        for row in picks.get(who) or ():
            if float(row.get("at") or 0.0) > since and table.get(str(row.get("key") or "")) in FIRST_ORDER:
                return True
    return False


def closed_quota_ok(today_keys: list[str], kinds: dict) -> bool:
    """再排一件閉環的，今天閉環的佔比會不會超過 1/3（算上這一件）。"""
    closed = sum(1 for key in today_keys if kinds.get(key) in CLOSED)
    return (closed + 1) * 3 <= len(today_keys) + 1


# ── ② 跨活動比產出 ──────────────────────────────────────────────────────────
def _grams(text: str) -> set[str]:
    body = "".join(ch for ch in str(text or "") if ch.isalnum())
    return {body[i:i + 2] for i in range(len(body) - 1)} if len(body) > 1 else ({body} if body else set())


def same_as_before(text: str, earlier) -> bool:
    wanted = _grams(text)
    if not wanted:
        return False
    for prior in earlier or ():
        other = _grams(prior)
        if other and len(wanted & other) / min(len(wanted), len(other)) >= NOVELTY_OVERLAP:
            return True
    return False


def note_output(state: dict, text: str, *, now: float) -> dict:
    """這套又收尾了一次：更新 `{"outputs": [...], "stale": n, "cooldown_until": t}` 並回它。

    跟最近 3 次太像 ⇒ `stale+1`、冷卻 2^(stale-1) 天（上限 7）；不像 ⇒ 歸零。空產出不算。
    """
    body = str(text or "").strip()
    row = dict(state or {})
    outputs = [str(item) for item in (row.get("outputs") or [])][-NOVELTY_WINDOW:]
    if not body:
        return row
    if same_as_before(body, outputs):
        stale = int(row.get("stale") or 0) + 1
        days = min(MAX_COOLDOWN_DAYS, 2 ** (stale - 1))
        row.update(stale=stale, cooldown_until=float(now) + days * 86400.0)
    else:
        row.update(stale=0, cooldown_until=0.0)
    row["outputs"] = (outputs + [body[:400]])[-NOVELTY_WINDOW:]
    return row


def cooling(state: dict | None, *, now: float) -> bool:
    return float((state or {}).get("cooldown_until") or 0.0) > float(now)


__all__ = ["CLOSED", "EXTERNAL", "FIRST_ORDER", "MAINTENANCE", "NONE", "OWN", "PEERS",
           "closed_quota_ok", "cooling", "has_fresh_material", "inputs_of", "is_closed",
           "note_output", "same_as_before"]
