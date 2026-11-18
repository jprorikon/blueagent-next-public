"""取代關係（GLOSSARY.md「取代」）的落庫動作：**兩側各一個哈希門牌**（owner 2026-11-01）。

形狀：

    舊的一側  superseded_by = <新的門牌>        （inbound：被誰取代）
    新的一側  supersedes    = <舊1>,<舊2>       （outbound：取代了誰）

為什麼要兩邊都存（而不是只留一邊、查的時候 join 反推）：

  · 舊的一側不刪、不改寫內容，只在它身上寫一個指針 ⇒ 「以前是什麼」永遠查得到；
  · 新的一側自己記住它換掉了哪幾條 ⇒ 不必掃全表才知道一條記憶的來歷；
  · 兩邊指針都在，`revive()` 只靠新那一側就能把舊的整批放回來 ⇒
    「刪掉替代者，舊的就復活」不需要重算任何東西（ADR-0014 的做法）。

這一支只管 SQL，不認識 `Fact`／記憶後端；`memory_sqlite` 與 `in_memory` 的呼叫端
把連接／清單交進來。門牌本身在 `brain/memory/facts.py`（`fact_key`）。
"""

from __future__ import annotations

import sqlite3
import time

from agent.brain.memory.facts import KEY_CHARS, fact_key, parse_keys

HEX = frozenset("0123456789abcdef")


def as_key(value) -> str:
    """原文／門牌（GLOSSARY.md「門牌」）→ 門牌。已經像門牌（16 位小寫十六進位）就原樣用。

    為什麼要認門牌：`supersede_slot(new_key=…)` 的既有呼叫點傳的是**原文**，
    而 `add()` 之後拿到的是門牌；兩種都要能吃，否則同一條事實會存下兩種鍵。
    """
    text = str(value or "").strip()
    if len(text) == KEY_CHARS and set(text) <= HEX:
        return text
    return fact_key(text)


def _scene(game) -> tuple[str, tuple]:
    """場景條件（2026-11-15）：取代**不跨場景**——桌上的新值只能收桌上的舊值、現實只收現實。

    `game=None`＝不限（維護工具 `tools/prune_memory.py` 明確指定門牌時用）。
    """
    return ("", ()) if game is None else (" AND COALESCE(game, 0) = ?", (int(game or 0),))


def retire_slot(db: sqlite3.Connection, *, entity: str, slot: str, new_key: str,
                at: float | None = None, game: int | None = None) -> int:
    """同槽位、**同場景**的現值全部收掉：`valid_to=now`、`superseded_by=新的門牌`。回傳筆數。"""
    key = as_key(new_key)
    if not str(slot or "").strip() or not key:
        return 0
    stamp = float(at if at is not None else time.time())
    scene, scene_args = _scene(game)
    rows = list(db.execute(
        "SELECT id FROM facts WHERE entity = ? AND slot = ?"
        " AND superseded_by = '' AND (valid_to = 0 OR valid_to > ?)" + scene,
        (str(entity or ""), str(slot), stamp, *scene_args)))
    for row in rows:
        db.execute("UPDATE facts SET valid_to = ?, superseded_by = ? WHERE id = ?",
                   (stamp, key, row["id"]))
    db.commit()
    return len(rows)


def settle_slot(db: sqlite3.Connection, *, entity: str, slot: str, key: str, at: float,
                game: int = 0) -> tuple[str, float]:
    """新事實要落進單值槽位之前：同槽、同場景**只留一條現值**，誰是現值**按事件時間**定。

    owner 2026-11-16「同一槽位保持一條，按時間關係刷新」。以前是「後寫的贏」：
    蒸餾不保證按時間順序跑（回填舊原文、補跑舊窗口），一條舊狀態晚到就會把較新的現值收掉。

      · 新來的較新（或一樣新）⇒ 舊的現值全部收掉，指向新來的（`key`）；回 `("", 0.0)`。
      · 庫裡有比它更新的 ⇒ 那一條繼續當現值；**新來的一落庫就是「已被取代」**，
        回 `(現值的門牌, 現值的時間)` 給呼叫端寫進新行。其餘較舊的現值也一併收給那一條。

    「時間」＝`valid_from`（沒有才用 `created_at`）。只動 `superseded_by`／`valid_to`／`supersedes`，不刪任何行。
    重建索引時按正本順序重放 `add()` 會得到同一個結果，所以這裡不必另記操作記錄。
    """
    key = as_key(key)
    if not str(slot or "").strip() or not key:
        return "", 0.0
    stamp = float(at or 0)
    rows = db.execute(
        "SELECT id, key, supersedes,"
        " CASE WHEN valid_from > 0 THEN valid_from ELSE created_at END AS happened"
        " FROM facts WHERE entity = ? AND slot = ? AND COALESCE(game, 0) = ? AND key != ?"
        " AND superseded_by = '' AND (valid_to = 0 OR valid_to > ?)",
        (str(entity or ""), str(slot), int(game or 0), key, stamp)).fetchall()
    if not rows:
        return "", 0.0
    newer = [row for row in rows if float(row["happened"] or 0) > stamp]
    winner = max(newer, key=lambda row: (float(row["happened"] or 0), row["id"])) if newer else None
    by_key, by_at = (str(winner["key"]), float(winner["happened"])) if winner is not None else (key, stamp)
    losers = [row for row in rows if winner is None or row["id"] != winner["id"]]
    for row in losers:
        db.execute("UPDATE facts SET superseded_by = ?,"
                   " valid_to = CASE WHEN valid_to = 0 OR valid_to > ? THEN ? ELSE valid_to END"
                   " WHERE id = ?", (by_key, by_at, by_at, row["id"]))
    if winner is not None:
        merged = sorted({*parse_keys(winner["supersedes"]), key,
                         *(str(row["key"]) for row in losers if row["key"])} - {by_key})
        db.execute("UPDATE facts SET supersedes = ? WHERE id = ?", (",".join(merged), winner["id"]))
    db.commit()
    return (by_key, by_at) if winner is not None else ("", 0.0)


def supersede(db: sqlite3.Connection, *, entity: str, keys, new_key: str,
              at: float | None = None, game: int | None = None) -> int:
    """把 `keys` 指到的幾條標成「已被 `new_key` 取代」（不刪）。回傳筆數。"""
    key = as_key(new_key)
    wanted = [as_key(item) for item in (keys or ())]
    wanted = [item for item in wanted if item and item != key]
    if not key or not wanted:
        return 0
    stamp = float(at if at is not None else time.time())
    marked = 0
    scene, scene_args = _scene(game)
    for old in wanted:
        cursor = db.execute(
            "UPDATE facts SET valid_to = CASE WHEN valid_to = 0 THEN ? ELSE valid_to END,"
            " superseded_by = ? WHERE entity = ? AND key = ? AND superseded_by = ''" + scene,
            (stamp, key, str(entity or ""), old, *scene_args))
        if int(cursor.rowcount or 0):
            marked += int(cursor.rowcount or 0)
            # 取代者已經在庫裡（維護工具／存量收斂事後補的取代）⇒ 另一側的指針也補上，`revive()` 才找得回來
            link_outbound(db, entity=entity, key=old, by=key)
    db.commit()
    return marked


def adopt_inbound(db: sqlite3.Connection, *, entity: str, key: str,
                  supersedes=(), game: int | None = None) -> tuple[str, ...]:
    """把「已經指向 `key` 的舊行」併進這一條的 `supersedes`（呼叫順序不影響結果）。

    `supersede_slot()` 有可能跑在 `add()` 之前（提煉器的既有順序）：那時新的那一條
    還不存在，只能先在舊的那一側寫指針。這裡把那些指針收回來，補齊另一側。
    """
    if not key:
        return tuple(supersedes or ())
    if game is not None:
        # 不變量的最後一道：**被取代者與取代者必須同場景**。呼叫端先 `supersede_slot()` 時還不知道
        # 新的那條是哪個場景（沒傳 `game`＝當成現實）⇒ 新的落庫後，把指錯場景的舊行放回現值。
        db.execute("UPDATE facts SET superseded_by = '', valid_to = 0"
                   " WHERE entity = ? AND superseded_by = ? AND COALESCE(game, 0) != ?",
                   (str(entity or ""), key, int(game or 0)))
        db.commit()
    scene, scene_args = _scene(game)
    inbound = [str(row["key"]) for row in db.execute(
        "SELECT key FROM facts WHERE entity = ? AND superseded_by = ?" + scene,
        (str(entity or ""), key, *scene_args))]
    merged = sorted({*(str(item) for item in (supersedes or ()) if str(item).strip()),
                     *(item for item in inbound if item and item != key)})
    return tuple(merged)


def link_outbound(db: sqlite3.Connection, *, entity: str, key: str, by: str) -> None:
    """這一條落庫時就帶著 `superseded_by`（晚到的舊值、重建時重放的行）⇒ 把它的門牌補進取代者的 `supersedes`。

    取代者還沒落庫（重放順序在後）就什麼都不做：它落庫時 `adopt_inbound` 會把這條收回去。
    """
    if not key or not by or key == by:
        return
    row = db.execute("SELECT id, supersedes FROM facts WHERE entity = ? AND key = ?",
                     (str(entity or ""), by)).fetchone()
    if row is None or key in parse_keys(row["supersedes"]):
        return
    db.execute("UPDATE facts SET supersedes = ? WHERE id = ?",
               (",".join(sorted({*parse_keys(row["supersedes"]), key})), row["id"]))
    db.commit()


def revive(db: sqlite3.Connection, *, key: str) -> int:
    """撤掉「`key` 取代了誰」：被它取代的回到現值，它自己的 `supersedes` 清空。"""
    target = as_key(key)
    if not target:
        return 0
    row = db.execute("SELECT supersedes FROM facts WHERE key = ?", (target,)).fetchone()
    olds = parse_keys(row["supersedes"]) if row is not None else ()
    revived = 0
    for old in olds:
        if old == target:
            continue
        cursor = db.execute(
            "UPDATE facts SET superseded_by = '', valid_to = 0 WHERE key = ? AND superseded_by = ?",
            (old, target))
        revived += int(cursor.rowcount or 0)
    if row is not None:
        db.execute("UPDATE facts SET supersedes = '' WHERE key = ?", (target,))
    db.commit()
    return revived


def backfill_keys(db: sqlite3.Connection) -> int:
    """舊行沒有門牌 ⇒ 用它們自己的文字現算一次；舊式（存原文）的 `superseded_by` 換成門牌。"""
    rows = db.execute("SELECT id, text, superseded_by FROM facts WHERE key = ''").fetchall()
    if not rows:
        return 0
    updates, relinks = [], []
    for row in rows:
        key = fact_key(str(row["text"]))
        if not key:
            continue
        updates.append((key, row["id"]))
        legacy = str(row["superseded_by"] or "")
        if legacy and as_key(legacy) != legacy:
            relinks.append((as_key(legacy), row["id"]))
    db.executemany("UPDATE facts SET key = ? WHERE id = ?", updates)
    db.executemany("UPDATE facts SET superseded_by = ? WHERE id = ?", relinks)
    db.commit()
    return len(updates)


__all__ = ["adopt_inbound", "as_key", "backfill_keys", "link_outbound", "retire_slot", "revive",
           "settle_slot",
           "supersede"]
