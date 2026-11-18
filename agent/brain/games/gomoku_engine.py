#!/usr/bin/env python3
"""五子棋的 **棋力引擎**（確定性、非語言模型）——難度旋鈕＋落子選擇。

從 `ba/game/gomoku.py` 拆出來（2026-10-20 模組化）——**行為零改**，`ba/game/gomoku.py`
仍然是對外 import 面（顯式 re-export 這裡的名字）。

為什麼是引擎而不是語言模型（沿用原本那段定調）：
① 語言模型讀棋盤不可靠（看錯局勢、下在已佔的格、前後矛盾）；
② 延遲（一局幾十手，每手等 3–6 s，節奏會崩）；
③ **強弱要能調**才好玩——引擎才有「難度」旋鈕。
她「講什麼」是另一件事（局勢摘要交給人格層），**不參與落子**。

難度＝三個旋鈕（下多深、防守多認真、失誤率多高）。**預設 `easy`**：owner 的原話是
「它太強了」⇒ 先給一檔「會擋四、會做三、但會漏」的棋，玩起來才有來有回。

零依賴（純 stdlib）。
"""
from __future__ import annotations

import random
from typing import Optional

from agent.brain.games.gomoku_board import Board, other

# ── 棋力 ──────────────────────────────────────────────────────────────────
#
# 三個旋鈕：`blunder`＝該贏/該擋卻沒看到的機率；`band`＝候選要達到最高分的幾成才進池子；
# `pool`＝池子上限（同分時隨機挑＝「不總是下最好那一手」）；`depth`＝2 時會試算一步、
# 避開「送了對手一個立即威脅」的點。
DIFFICULTY = {
    # 預設檔：會擋四、會做三，但**會漏**（owner：「它太強了」）。
    "easy": dict(blunder=0.35, band=0.55, pool=6, depth=1, defense=0.75),
    "normal": dict(blunder=0.12, band=0.75, pool=3, depth=1, defense=0.95),
    "hard": dict(blunder=0.00, band=1.00, pool=1, depth=2, defense=1.00),
}
DEFAULT_DIFFICULTY = "easy"


def _pool(board: Board, side: str, cfg: dict, rng: random.Random) -> list[tuple[int, int]]:
    cands = board.candidates() or [(board.size // 2, board.size // 2)]
    scored = []
    for (x, y) in cands:
        attack = board.placement_value(x, y, side)
        defend = board.placement_value(x, y, other(side))
        scored.append((attack + cfg["defense"] * defend, x, y))
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    best = scored[0][0]
    cutoff = best * cfg["band"] if best > 0 else 0.0
    pool = [(x, y) for score, x, y in scored if score >= cutoff][: cfg["pool"]]
    return pool or [(scored[0][1], scored[0][2])]


def _gift(board: Board, x: int, y: int, side: str) -> float:
    """下在 (x,y) 之後，對手是否立刻拿到「成五」或「活四」——`hard` 用它避開自殺點。

    回一個扣分（0＝這手安全）。**順序很重要**：先落子、再看對手的威脅、再撤回。
    """
    board.play(x, y, side)
    try:
        foe = board.threats(other(side))
        if foe["five"]:
            return 10 ** 8
        if foe["four"]:
            return 10 ** 4
        # 對手下一手能做出活三 ⇒ 半個警告（不是致命的，只是局面會變難看）。
        return 1.0 * len(foe["open_three"])
    finally:
        board.undo()


def choose_move(board: Board, difficulty: str = DEFAULT_DIFFICULTY,
                rng: Optional[random.Random] = None) -> Optional[tuple[int, int]]:
    """回一手合法的棋（或 `None`＝真的下滿了）。**不改動傳進來的棋盤。**

    順序：我先成五 → 擋對手的五 → 按分數挑候選（含失誤率與深度過濾）。
    `easy` 的「漏」就發生在第一、二步：`blunder` 的機率下它連自己的勝手／對手的殺手都會漏掉。
    """
    if board.over:
        return None
    rng = rng or random.Random()
    cfg = DIFFICULTY.get(difficulty, DIFFICULTY[DEFAULT_DIFFICULTY])
    side, foe = board.turn, other(board.turn)

    if not board.cells:
        center = board.size // 2
        return (center, center)

    # ① 我現在就能贏
    wins = board.threats(side)["five"]
    if wins and rng.random() >= cfg["blunder"]:
        return wins[0]
    # ② 對手的五要擋（**擋棋比取勝更容易被漏**：一半的失誤率）
    blocks = board.threats(foe)["five"]
    if blocks and rng.random() >= cfg["blunder"] * 0.5:
        return blocks[0]

    # ③ 打分挑候選
    pool = _pool(board, side, cfg, rng)
    if cfg["depth"] >= 2 and len(pool) > 1:
        ranked = sorted(pool, key=lambda p: (-(board.placement_value(*p, side)) + _gift(board, *p, side)))
        pool = ranked[:1] or pool
    return rng.choice(pool)
