"""門牌（pointer）：摘要 → 原文的可回取指針。

為什麼要有它（2026-10-25 從 `cyanwell/dsh-agent-soul` 的 ECHO 判據借來，只借判據不借代碼）：

    壓縮摘要每一條都可以「對」，但都不帶出處
      ⇒ 下一輪推理把偏了半個字的前提當成事實，而人不核對（摘要工作得太好了）
      ⇒ **退化不是變笨，是在錯誤的前提上繼續正確地推理。**

所以任何壓縮／提煉產物都要能答一句：「這一條從哪來」。三級紀律：

    ① 拿得到精確位置 → 寫精確位置
    ② 拿不到 → 寫檢索入口（entity ＋ 關鍵詞），**不寫半個門牌**
    ③ 絕不猜 —— 錯的門牌比沒有更壞（它會讓你以為找得到，於是再也不去找）

本倉的格式（`raw:` 命名空間，指向 `data/raw/<entity>/<YYYY-MM>.jsonl` 的**逐字原文**）：

    raw:<entity>#t=<turn_id>          單輪
    raw:<entity>#t=<turn_id>+<n>      從這一輪起、**同一條線**上連續 n 個不同的輪次（窗口摘要用）

「輪」是 turn id，不是行：原文裡一輪至少兩行（她聽到的、她說的），而且所有線
按寫入時間混寫在同一份月檔裡。所以 `+n` 只能數「同一條線上的不同 turn id」——
數行會把別條線（例如群聊）插進來的行當成這一段，同時把這一段的尾巴擠掉（2026-10-26 修）。

判據（測試在 `tests/test_pointer.py`）：
  ① `make()` 只在真的拿到 turn id 時才吐門牌，否則回空字串；
  ② `resolve()` 找不到就明說 `turn_not_found`，**不拿鄰行充數**；
  ③ `open` 回的是逐字原文（含 `room`，披露規則留給呼叫端判）。
"""

from __future__ import annotations

import json
import pathlib
import re

#: 門牌前綴。換檢索系統也能用——只要它認這一種形式。
PREFIX = "raw:"

_PATTERN = re.compile(r"^raw:(?P<entity>[^#\s]+)#t=(?P<turn>[0-9A-Za-z_.:-]+)(?:\+(?P<count>\d+))?$")
#: 在一段文字裡找門牌（不要求整串等於門牌：摘要會把門牌包在〔〕或句子裡）
_ANY = re.compile(r"raw:[^#\s]+#t=[0-9A-Za-z_.:-]+(?:\+\d+)?")


#: 這些「字串化的空值」不可以當 turn id（舊產物曾寫出 `[null, null, …]`，
#: `str(None)` ＝ `"None"`，會被當成合法 id ⇒ 造出**打不開的假門牌**）。
_JUNK_IDS = {"", "none", "null", "nan", "undefined", "0"}


def _clean_id(item) -> str:
    if item is None:
        return ""
    text = str(item).strip()
    return "" if text.lower() in _JUNK_IDS else text


def make(entity: str, turn_ids) -> str:
    """把一組 turn id 壓成一個門牌。**拿不到就回空字串**（不回半個門牌）。

    `+n` 數的是**不同的** turn id：提煉器按原文行傳進來（一輪兩行＝同一個 id 兩次），
    封片按 TurnRecord 傳進來——兩邊都要得到同一個 n。
    """
    entity = str(entity or "").strip()
    ids = list(dict.fromkeys(item for item in (_clean_id(value) for value in (turn_ids or ()))
                             if item))
    if not entity or not ids:
        return ""
    head = ids[0]
    if len(ids) == 1:
        return f"{PREFIX}{entity}#t={head}"
    return f"{PREFIX}{entity}#t={head}+{len(ids)}"


def parse(text: str) -> dict | None:
    """門牌 → `{entity, turn, count, text}`；格式不對回 `None`（呼叫端要當「沒門牌」處理）。"""
    match = _PATTERN.match(str(text or "").strip())
    if match is None:
        return None
    count = int(match.group("count") or 1)
    return {"entity": match.group("entity"), "turn": match.group("turn"),
            "count": max(1, count), "text": match.group(0)}


def looks_like(text: str) -> bool:
    """這段文字裡有沒有門牌（給「摘要帶不帶指針」的體檢用）。"""
    return bool(_ANY.search(str(text or "")))


def extract(text: str) -> list[str]:
    """把一段文字裡的所有門牌撈出來（順序保留、去重）。"""
    seen: list[str] = []
    for match in _ANY.finditer(str(text or "")):
        if match.group(0) not in seen:
            seen.append(match.group(0))
    return seen


def scan_rows(raw_dir, entity: str):
    """逐行串流 `data/raw/<entity>/*.jsonl`，壞行跳過（不讓一行壞掉毀掉整個回取）。"""
    folder = pathlib.Path(raw_dir) / str(entity or "")
    if not entity or not folder.is_dir():
        return
    for path in sorted(folder.glob("*.jsonl")):
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row


def resolve(text: str, raw_dir, *, max_turns: int | None = None) -> dict:
    """門牌 → 原文那幾輪（逐字，不轉述）。

    回 `{ok, reason, entity, anchor, count, thread, turns, rows, missing, truncated, next_turn}`：
      · `ok=False` + `bad_pointer`    格式不對／entity 對不上；
      · `ok=False` + `turn_not_found` 原文裡找不到這一輪（**不猜鄰行**）；
      · `missing>0`                   指針要求 n 輪，原文只有 n-k 輪（回填未完成）；
      · `truncated`                   超過 `max_turns` 就停（`next_turn` 是下一輪的 id，可接著開）。

    只收**起點那一行所在的線**；別條線插在中間的行一律跳過（它們不是這一段）。
    """
    parsed = parse(text)
    if parsed is None:
        return {"ok": False, "reason": "bad_pointer", "rows": [], "missing": 0}
    entity, anchor, count = parsed["entity"], parsed["turn"], parsed["count"]
    limit = count if max_turns is None else max(1, min(count, int(max_turns)))
    rows: list[dict] = []
    turns: list[str] = []
    thread = None
    next_turn = ""
    for row in scan_rows(raw_dir, entity):
        turn = str(row.get("turn") or "")
        if thread is None:
            if turn != anchor:
                continue
            thread = str(row.get("thread") or "")
        elif str(row.get("thread") or "") != thread:
            continue                                  # 別條線插進來的行：不是這一段
        if turn not in turns:
            if len(turns) >= limit:
                next_turn = turn if limit < count else ""
                break
            turns.append(turn)
        rows.append(row)
    if not rows:
        return {"ok": False, "reason": "turn_not_found", "entity": entity,
                "anchor": anchor, "count": count, "rows": [], "missing": count}
    truncated = bool(next_turn)
    return {"ok": True, "reason": "", "entity": entity, "anchor": anchor, "count": count,
            "thread": thread or "", "turns": len(turns), "rows": rows,
            "missing": 0 if truncated else max(0, count - len(turns)),
            "truncated": truncated, "next_turn": next_turn}


def coverage(rows, *, field: str = "ptr") -> dict:
    """門牌覆蓋率：`{total, covered, ratio}`。體檢與測試共用，避免兩處各算一遍。"""
    total = 0
    covered = 0
    for row in rows or ():
        total += 1
        value = ""
        try:
            value = str(row[field] if not isinstance(row, dict) else row.get(field) or "")
        except (KeyError, IndexError, TypeError):
            value = ""
        if value.strip():
            covered += 1
    return {"total": total, "covered": covered,
            "ratio": (covered / total) if total else 0.0}


__all__ = ["PREFIX", "coverage", "extract", "looks_like", "make", "parse", "resolve",
           "scan_rows"]
