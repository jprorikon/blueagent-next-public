"""`memory.open`：按門牌把**逐字原文**取回來（不轉述、不猜鄰行）。

判據：① 它必須吃得下 `memory.search` 自己吐出來的 `ptr`（否則門牌只是一串好看的代號）；
      ② 找不到就回 `turn_not_found`，不是「大概是這句」；
      ③ 披露規則與檢索同一條（`runtime/memory/disclosure.py`：在場的人都在房間裡才給）；
      ④ 一次回的量有上限，超過就給 `more`（下一扇門），連續翻頁不重複、不遺漏。

從 `memory_search.py` 拆出來（2026-10-26）：那支檔加上分頁超過 600 行上限，
而「按門牌開門」本來就是獨立的一件事——它不碰向量、不碰排序。
"""

from __future__ import annotations

import os

from agent.brain.memory.facts import game_of
from agent.brain.runtime.memory import pointer as pointers
from agent.brain.runtime.memory.disclosure import disclosable

#: 一次最多回幾輪／幾行：門牌的 `+n` 是模型抄進來的，`+999999` 會把
#: 整份原文塞進下一輪請求（工具結果不截斷）。超過就停，回 `more` 讓她接著開。
OPEN_MAX_TURNS = int(os.environ.get("BLUEAGENT_OPEN_MAX_TURNS") or 20)
OPEN_MAX_ROWS = int(os.environ.get("BLUEAGENT_OPEN_MAX_ROWS") or 60)


def open_pointer(raw_dir, *, entity: str, present, pointer: str,
                 game: int | None = None) -> dict:
    """門牌 → `{ok, pointer, rows, missing, truncated[, more]}` 或 `{ok: False, error}`。

    `game`（`0`＝現實回合、`1`＝桌上回合、`None`＝不限）：另一個場景的行一律當不存在
    ——否則拿著以前見過的門牌就能繞過 `memory.search` 的場景過濾。
    """
    parsed = pointers.parse(pointer)
    if parsed is None:
        return {"ok": False, "error": "bad_pointer"}
    if str(parsed["entity"]) != str(entity or ""):
        # 實體分倉是硬不變量：別人的門牌一律當不存在（不區分「有但不能看」）
        return {"ok": False, "error": "turn_not_found"}
    if raw_dir is None:
        return {"ok": False, "error": "no_raw_log"}
    found = pointers.resolve(parsed["text"], raw_dir, max_turns=OPEN_MAX_TURNS)
    if not found.get("ok"):
        return {"ok": False, "error": str(found.get("reason") or "turn_not_found"),
                "pointer": parsed["text"]}
    people = {str(who).strip() for who in (present or ()) if str(who).strip()}
    people.add(str(entity or ""))
    rows = [{"turn": row.get("turn"), "at": row.get("at"), "role": row.get("role"),
             "speaker": row.get("speaker"), "text": str(row.get("text") or ""),
             "thread": row.get("thread")}
            for row in found["rows"] if disclosable(people, row.get("room"))
            and (game is None or game_of(row.get("source"), row.get("purpose")) == int(game))]
    if not rows:
        return {"ok": False, "error": "turn_not_found", "pointer": parsed["text"]}
    # 原文行裡的輪次順序（含不能給看的行：它們一樣佔掉門牌的輪數）
    order = list(dict.fromkeys(str(row.get("turn") or "") for row in found["rows"]))
    kept = _clip_at_turn(rows, OPEN_MAX_ROWS)
    next_turn = str(found.get("next_turn") or "")
    if len(kept) < len(rows):
        # 被行數上限切掉：下一扇門從「最後一個回出去的輪次」的下一輪開始
        position = order.index(str(kept[-1].get("turn") or "")) + 1
        next_turn = order[position] if position < len(order) else next_turn
    out = {"ok": True, "pointer": parsed["text"], "rows": kept,
           "missing": int(found.get("missing") or 0), "truncated": bool(next_turn)}
    if next_turn:
        # 還沒開完：給她下一扇門（同一條線、剩下的輪數），要看再開
        used = order.index(next_turn) if next_turn in order else int(found.get("turns") or 0)
        rest = int(found["count"]) - used
        out["more"] = (f"{pointers.PREFIX}{parsed['entity']}#t={next_turn}"
                       + (f"+{rest}" if rest > 1 else ""))
    return out


def _clip_at_turn(rows: list[dict], cap: int) -> list[dict]:
    """最多回 `cap` 行，**只在輪次邊界切**——下一扇門從下一輪開始，才不重複、不遺漏。

    一輪自己就超過 `cap` 行（實際不會：一輪是她聽到的＋她說的）⇒ 回前 `cap` 行，
    門牌只能指到輪，那一輪剩下的行無法再翻到。
    """
    cap = max(1, int(cap))
    if len(rows) <= cap:
        return rows
    kept: list[dict] = []
    index = 0
    while index < len(rows):
        turn = str(rows[index].get("turn") or "")
        end = index
        while end < len(rows) and str(rows[end].get("turn") or "") == turn:
            end += 1
        if len(kept) + (end - index) > cap:
            return kept or rows[:cap]
        kept.extend(rows[index:end])
        index = end
    return kept


__all__ = ["OPEN_MAX_ROWS", "OPEN_MAX_TURNS", "open_pointer"]
