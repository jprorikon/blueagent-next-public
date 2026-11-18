"""蒸餾的輔助函式：**遊戲窗口的標記**、窗口指紋與候選事實落庫（把 distill.py 壓在 600 行棘輪下）。

為什麼要單獨一支：`runtime/distill.py` 已經頂到 600 行上限（結構 review 的棘輪只准變短），
而 2026-11-11 的 B3（TRPG／現實在記憶層分家）與 2026-11-15 WP7 把**純函式與候選事實落庫步驟**搬出來，
心臟那支只留窗口調度流程。
"""

from __future__ import annotations

import hashlib
import os
import time

from agent.brain.memory.fact_rules import ADDRESS_SLOT, canonical_address
from agent.brain.memory.facts import game_of, normalize_text

#: 允許寫進記憶的層（提煉只能落這幾種；`world`/`self` 由別的路徑產生）。
ALLOWED_LAYERS = {"pair", "circle", "consensus", "private"}
#: 事件型記憶的預設壽命（天）：模型沒給 `valid_days` 時用這個。
#: owner 2026-11-16：1 → 3 天。也管她公開活動日誌那條 sink（`compose/wiring.py`）。
EVENT_TTL_DAYS = float(os.environ.get("BLUEAGENT_EVENT_TTL_DAYS") or 3.0)

#: 給提煉模型的一條規則（只有在 TRPG 窗口才有意義；接在 PROMPT 尾巴）
GAME_PROMPT_RULE = """
⚠️ このウィンドウが **TRPG 卓（ゲームの中＝架空）** のとき：
  卓上の出来事・台詞は虚構。事実として残してよいのは「主人と TRPG を遊んだ」「その局でこうなった（ゲームの中）」の層まで。
  卓上の台詞を「主人が言った」として書かない。主人の好み・状態・約束として書かない（ゲームの中の役どころと現実の人を混ぜない）。
"""

#: 貼在窗口原文前面的說明（沒有它，提煉模型看到的只是「主人：…」）
GAME_HEADER = "（このウィンドウは TRPG 卓。以下はゲームの中の出来事で、現実ではない）"


def is_game_row(row: dict) -> bool:
    """這一輪是不是桌（TRPG）上的：`purpose=trpg`（B1 之後 raw 有）或 `source=trpg:*`。"""
    return bool(game_of(row.get("source"), row.get("purpose")))


def window_game(rows) -> int:
    """這一窗算不算遊戲：過半是桌上的輪次 ⇒ `1`（否則 `0`）。

    窗口本來就按 thread 切、正常整窗一致；用「過半」是為了舊資料沒有 `purpose` 時，
    也能靠 `source=trpg:*` 認出來。
    """
    rows = list(rows or ())
    if not rows:
        return 0
    return 1 if sum(1 for row in rows if is_game_row(row)) * 2 >= len(rows) else 0


def window_signature(session: dict) -> str:
    """窗口指紋：輪數 ＋ 內容 hash。內容變了就重跑（回填新原文、補對話）。"""
    rows = session.get("rows") or []
    digest = hashlib.sha1()
    for row in rows:
        digest.update(str(row.get("turn") or row.get("at") or "").encode("utf-8"))
        digest.update(str(row.get("text") or "").encode("utf-8"))
    return f"{len(rows)}:{digest.hexdigest()[:16]}"


def event_valid_to(kind: str, valid_days: float, happens_at: float) -> float:
    """事件型回「失效時間」；非事件回 0（永久有效）。

    模型沒給 `valid_days` ⇒ 用 `EVENT_TTL_DAYS`（預設 3 天）：
    「今晚準備角色卡」這種事，明天還掛在她嘴邊就是錯的。
    """
    if str(kind or "").strip().lower() != "event":
        return 0.0
    lifespan = float(valid_days or 0) or EVENT_TTL_DAYS
    return float(happens_at or 0) + lifespan * 86400.0


#: 提煉 prompt 裡最多列幾個現用槽位名
SLOT_HINT_LIMIT = int(os.environ.get("BLUEAGENT_DISTILL_SLOT_HINTS") or 20)


def slot_hint(memory, entity: str, game: int, *, limit: int = SLOT_HINT_LIMIT) -> str:
    """給提煉模型看「這個角色現在用著哪些槽位名」（同場景、最近寫入的在前）。沒有就回空字串。

    為什麼要給：槽位名是模型自己寫的自由文字，同一件事這次叫「主人がいま読んでいる本」、
    下次叫「主人の読書」就成了兩個槽 ⇒ 「同一槽位保持一條」收不到它們。
    看得到現用的名字，同一個枠才會沿用同一個名字。
    """
    if limit <= 0:
        return ""
    try:
        local = getattr(memory, "local", None) or memory
        rows = local.snapshot(None, entity=entity).get("facts", []) if hasattr(local, "snapshot") else []
    except Exception:                                       # noqa: BLE001 - 提示拿不到不擋提煉
        return ""
    rows = [row for row in rows if row.get("active") and str(row.get("slot") or "").strip()
            and str(row.get("slot")) != ADDRESS_SLOT and int(row.get("game") or 0) == int(game or 0)]
    rows.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
    names = list(dict.fromkeys(str(row["slot"]).strip() for row in rows))[:limit]
    if not names:
        return ""
    return ("いま使われている slot の名前（**同じ枠ならこの名前を一字一句そのまま使う**。"
            "新しい枠のときだけ新しい名前を付ける）：\n"
            + "\n".join(f"  - {name}" for name in names) + "\n\n")


def with_slot_hint(prompt: str, hint: str, *, marker: str = "会話ウィンドウ：") -> str:
    """把槽位提示插在「会話ウィンドウ：」那一行之前（找不到標記就接在最後）。"""
    if not hint:
        return prompt
    head, found, tail = prompt.rpartition(marker)
    return head + hint + found + tail if found else prompt + hint


def apply_candidates(distiller, session: dict, candidates: list[dict], *,
                     entity: str, run_id: str, history: list[dict],
                     summary: dict) -> list[str]:
    """判定一個窗口的候選事實（門檻／噪音／去重／槽位取代）並落庫，回傳寫入的 `fact_key` 清單。"""
    touched: list[str] = []
    game = int(session.get("game") or 0)
    for candidate in candidates:
        text = distiller.normalize_names(str(candidate.get("text") or ""))
        if not text:
            continue
        # 稱呼類事實一律收斂到權威寫法（owner 2026-11-03「收斂到 オーナー」）：
        # 庫裡 190 條「船長↔オーナー」互相打架，撈到哪條她就叫哪個。
        # 改寫成固定一句 ⇒ 同一句＝同一 norm ⇒ 下面 `_exists` 與 `add()` 本來就會擋重複，
        # 不會再堆第二條；槽位再綁死，新的自動把舊的收掉（見下方 `addressed`）。
        canonical = canonical_address(text)
        addressed = canonical != text
        text = canonical
        try:
            confidence = float(candidate.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        layer = str(candidate.get("layer") or "pair").strip().lower()
        if layer not in ALLOWED_LAYERS:
            layer = "pair"
        fact_key = normalize_text(text)
        evidence = [str(item) for item in (candidate.get("evidence") or [])]
        row = {"fact_key": fact_key, "entity": entity, "confidence": confidence,
               "evidence": evidence, "run_id": run_id, "at": distiller._clock()}
        skip = _skip_reason(distiller, entity, text, fact_key, confidence, touched)
        if skip:
            history.append({**row, "action": "skip", "reason": skip})
            summary["facts_skipped"] += 1
            continue
        kind = str(candidate.get("kind") or "").strip().lower()
        try:
            valid_days = float(candidate.get("valid_days") or 0)
        except (TypeError, ValueError):
            valid_days = 0.0
        happens_at = float(session.get("ended_at") or 0) or time.time()
        slot = str(candidate.get("slot") or "").strip()
        if addressed:
            # 稱呼是「規則」不是「狀態」，內容已被固定成 canonical
            kind, slot = "rule", ADDRESS_SLOT
        valid_to = event_valid_to(kind, valid_days, happens_at)
        # 同槽位的舊現值由記憶後端的 `add()` 收（`supersede.settle_slot`，owner 2026-11-16）：
        # 按**事件時間**定誰是現值，不是「後寫的贏」——這裡不再先呼叫 `supersede_slot`，
        # 否則補跑的舊窗口會把較新的現值收掉。舊資料不刪，as-of 還查得到。
        # 模型自己說「這條取代了哪些舊事實」⇒ 兩側各寫一個門牌指針
        replacing = distiller._resolve_supersedes(entity, candidate.get("supersedes"), game=game)
        distiller.memory.add(text, layer=layer, audience=set(session["room"] or {entity}),
                             label="提煉", principal=(session["participants"] or [""])[0],
                             source="distill", entity=entity, kind=kind,
                             valid_from=happens_at, valid_to=valid_to, slot=slot,
                             # 場景維度（2026-11-11 B3）：桌（虛構）那幾窗抽出來的事實打標
                             supersedes=replacing, prior=confidence, game=game)
        touched.append(fact_key)
        distiller._inserted.setdefault(entity, []).append(text)
        history.append({**row, "action": "insert", "reason": ""})
        summary["facts_added"] += 1
    return touched


def _skip_reason(distiller, entity: str, text: str, fact_key: str,
                 confidence: float, touched: list[str]) -> str:
    if confidence < distiller.min_conf:
        return "below_threshold"
    noise = distiller.noise_reason(text)
    if noise:
        return noise
    if distiller._near_duplicate(entity, text) or distiller._near_inserted(entity, text):
        return "near_duplicate"
    if fact_key in touched:
        return "duplicate_in_run"
    if distiller._exists(entity, text):
        return "duplicate_existing"
    return ""


__all__ = ["ALLOWED_LAYERS", "EVENT_TTL_DAYS", "GAME_HEADER", "GAME_PROMPT_RULE",
           "SLOT_HINT_LIMIT", "apply_candidates", "event_valid_to", "is_game_row", "slot_hint",
           "window_game", "window_signature", "with_slot_hint"]
