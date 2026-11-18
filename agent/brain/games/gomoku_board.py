#!/usr/bin/env python3
"""五子棋的 **規則層**：棋盤狀態＋勝負判定（`Board`＝唯一規則權威）。

從 `ba/game/gomoku.py` 拆出來（2026-10-20 模組化）——**行為零改**，`ba/game/gomoku.py`
仍然是對外 import 面（顯式 re-export 這裡的名字）。

紀律（沿用原本那段定調）：**規則只有一份，在橋側**。手機只畫，不判棋
（同專案既有紀律：政策在共用層／權威在一處；兩份真相一定分叉）。

零依賴（純 stdlib）。
"""
from __future__ import annotations

from typing import Optional

SIZE = 15
BLACK, WHITE = "black", "white"
DIRECTIONS = ((1, 0), (0, 1), (1, 1), (1, -1))

# 一條線上「連了幾顆」值多少。五連是勝負本身 ⇒ 給一個壓過一切的數字（引擎看到一定拿）。
RUN_VALUE = {5: 10 ** 7, 4: 10 ** 5, 3: 10 ** 3, 2: 20, 1: 1}


class Illegal(Exception):
    """不合法的落子（越界／已佔／不是他的輪／棋已經結束）。"""


def other(side: str) -> str:
    return WHITE if side == BLACK else BLACK


def _ends_factor(open_ends: int) -> float:
    """兩頭都空 ＝ 活（1.6）、一頭被堵 ＝ 半死（1.0）、兩頭都被堵 ＝ 死（0.35）。

    這三個數字是**口味**不是定理：它們決定引擎「願不願意做三」。活三要真的做出來，
    死線不值得花一手——但也不能當成 0，不然填自己空位那種棋會全部消失。
    """
    return (0.35, 1.0, 1.6)[min(open_ends, 2)]


class Board:
    """一局棋的狀態。**這裡是唯一的規則權威**（手機不重算勝負）。"""

    def __init__(self, size: int = SIZE, first: str = BLACK):
        self.size = size
        self.first = first
        self.cells: dict[tuple[int, int], str] = {}
        self.history: list[tuple[int, int, str]] = []
        self.turn = first
        self.winner: Optional[str] = None   # None / "black" / "white" / "draw"
        self.over = False
        self.last: Optional[tuple[int, int, str]] = None

    # ── 讀 ────────────────────────────────────────────────────────────────
    def cell(self, x: int, y: int) -> Optional[str]:
        """`"black"`／`"white"`／`""`（空）／`None`（棋盤外）。**空與界外要分開**——
        引擎判斷「這條線還活著嗎」靠的就是這個差別。"""
        if not (0 <= x < self.size and 0 <= y < self.size):
            return None
        return self.cells.get((x, y), "")

    def stone(self, x: int, y: int) -> Optional[str]:
        value = self.cell(x, y)
        return value or None

    def legal(self, x: int, y: int) -> bool:
        return (not self.over) and self.cell(x, y) == ""

    def move_no(self) -> int:
        return len(self.history)

    def candidates(self, radius: int = 2) -> list[tuple[int, int]]:
        """空點中「離既有棋子（切比雪夫距離）≤ radius」的那些——引擎不必看整個棋盤。

        空盤時回空清單（呼叫端自己決定天元）。
        """
        if not self.cells:
            return []
        found: list[tuple[int, int]] = []
        for y in range(self.size):
            for x in range(self.size):
                if self.cell(x, y) != "":
                    continue
                near = any(
                    self.stone(x + dx, y + dy)
                    for dx in range(-radius, radius + 1)
                    for dy in range(-radius, radius + 1)
                )
                if near:
                    found.append((x, y))
        return found

    def run_through(self, x: int, y: int, side: str) -> tuple[int, int]:
        """假設 `side` 下在 (x,y)：回（最長的那條連線長度, 那條線的開放端數）。

        只給**棋力**用，不判勝負——勝負一律走 [wins_from]／[play]。
        """
        best_run, best_open = 1, 0
        for dx, dy in DIRECTIONS:
            run, open_ends = 1, 0
            for sign in (1, -1):
                step = 1
                while self.cell(x + sign * dx * step, y + sign * dy * step) == side:
                    run += 1
                    step += 1
                if self.cell(x + sign * dx * step, y + sign * dy * step) == "":
                    open_ends += 1
            if run > best_run or (run == best_run and open_ends > best_open):
                best_run, best_open = run, open_ends
        return min(best_run, 5), best_open

    def placement_value(self, x: int, y: int, side: str) -> float:
        """「在這放一顆」值多少：四個方向各自算連線價值，加起來。

        這是**啟發式**（貪心看單點），不是搜索——文件寫明白，免得後來的人以為它證明了什麼。
        """
        if self.cell(x, y) != "":
            return -1.0
        total = 0.0
        for dx, dy in DIRECTIONS:
            run, open_ends = 1, 0
            for sign in (1, -1):
                step = 1
                while self.cell(x + sign * dx * step, y + sign * dy * step) == side:
                    run += 1
                    step += 1
                if self.cell(x + sign * dx * step, y + sign * dy * step) == "":
                    open_ends += 1
            total += RUN_VALUE[min(run, 5)] * _ends_factor(open_ends)
        return total

    def threats(self, side: str) -> dict[str, list[tuple[int, int]]]:
        """`side` **下一手**能做出的威脅，按種類分開：

        * `five`——下一手就五連（＝他已經贏了，除非你擋）；
        * `four`——下一手成四連（擋不住的那種會出現在這裡，因為四連本身就是「下一手成五」的前一步）；
        * `open_three`——下一手成活三（兩頭都空的連三）。

        同一格可能同時算 `four` 與 `open_three`（例如連四兩頭空）——**不清洗**，
        因為「她搭話」要的正是原樣的事實，引擎各取所需。
        """
        found: dict[str, list[tuple[int, int]]] = {"five": [], "four": [], "open_three": []}
        if self.over:
            return found
        for (x, y) in self.candidates():
            run, open_ends = self.run_through(x, y, side)
            if run >= 5:
                found["five"].append((x, y))
            elif run == 4:
                found["four"].append((x, y))
            elif run == 3 and open_ends == 2:
                found["open_three"].append((x, y))
        return found

    # ── 寫 ────────────────────────────────────────────────────────────────
    def play(self, x: int, y: int, side: Optional[str] = None) -> tuple[int, int, str]:
        """落子。不合法一律 [Illegal]（**不**靜默忽略——橋側要靠這條擋掉越界／重複／搶手）。"""
        who = side or self.turn
        if who not in (BLACK, WHITE):
            raise Illegal(f"unknown side: {who!r}")
        if self.over:
            raise Illegal("game is over")
        if side and side != self.turn:
            raise Illegal(f"not {side}'s turn (it is {self.turn}'s)")
        if self.cell(x, y) != "":
            raise Illegal(f"({x},{y}) is not a playable point")
        self.cells[(x, y)] = who
        self.history.append((x, y, who))
        self.last = (x, y, who)
        if self._wins_from(x, y, who):
            self.winner, self.over = who, True
        elif len(self.cells) == self.size * self.size:
            self.winner, self.over = "draw", True
        else:
            self.turn = other(who)
        return self.last

    def undo(self) -> Optional[tuple[int, int, str]]:
        """撤回最後一手（引擎試算／測試用）。勝負與輪次要一起還原。"""
        if not self.history:
            return None
        x, y, who = self.history.pop()
        del self.cells[(x, y)]
        self.last = self.history[-1] if self.history else None
        self.winner, self.over = None, False
        self.turn = who
        return (x, y, who)

    def copy(self) -> "Board":
        clone = Board(self.size, self.first)
        clone.cells = dict(self.cells)
        clone.history = list(self.history)
        clone.turn, clone.winner, clone.over, clone.last = self.turn, self.winner, self.over, self.last
        return clone

    def _wins_from(self, x: int, y: int, side: str) -> bool:
        """剛下的那顆 (x,y) 是否連成五（只看穿過它的四條線）。"""
        for dx, dy in DIRECTIONS:
            run = 1
            for sign in (1, -1):
                step = 1
                while self.stone(x + sign * dx * step, y + sign * dy * step) == side:
                    run += 1
                    step += 1
            if run >= 5:
                return True
        return False

    # ── 給外部的兩種投影 ──────────────────────────────────────────────────
    def situation(self) -> dict:
        """**事實**（不是措辭）：等一下要交給人格層講成一句話的就是這些。"""
        return {
            "move_no": self.move_no(),
            "turn": self.turn,
            "over": self.over,
            "winner": self.winner,
            "last": list(self.last) if self.last else None,
            "black_threats": {k: [list(p) for p in v] for k, v in self.threats(BLACK).items()},
            "white_threats": {k: [list(p) for p in v] for k, v in self.threats(WHITE).items()},
        }

    def snapshot(self) -> dict:
        """JSON-able 盤面（＝app 的 `game` 幀要畫的東西）。`stones` 用稀疏清單，15×15 不必塞 225 格。"""
        return {
            "size": self.size,
            "stones": [[x, y, side] for (x, y), side in sorted(self.cells.items())],
            "turn": self.turn,
            "move_no": self.move_no(),
            "last": list(self.last) if self.last else None,
            "winner": self.winner,
            "over": self.over,
        }

    def ascii(self) -> str:
        head = "   " + "".join(str(x % 10) for x in range(self.size))
        rows = [head]
        for y in range(self.size):
            row = f"{y:2d} "
            for x in range(self.size):
                side = self.cells.get((x, y))
                row += "." if not side else ("X" if side == BLACK else "O")
            rows.append(row)
        return "\n".join(rows)
