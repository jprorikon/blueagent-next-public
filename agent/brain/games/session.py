"""五子棋的「一局」：**用完即棄、與會話無關**。

照舊橋的兩條硬規則（設計 §0-B）：

  · **與會話無關**：不碰 history／soul／記憶／device-note，也不寫檔；
  · **用完即棄**：`close` 之後那一局就沒了（重開是另一局）。

分工：規則與棋力在 `gomoku_board`／`gomoku_engine`（伺服器唯一權威），
這裡只管「誰在跟誰下、輪到誰、怎麼收」，並把棋盤攤成客戶端畫得出來的形狀。
落子**不經語言模型**（模型只在她搭話那條路上用）。
"""

from __future__ import annotations

import threading
import time
import uuid

from agent.brain.games.gomoku_board import BLACK, WHITE, Board
from agent.brain.games.gomoku_engine import choose_move

#: 一局最多活多久（小時級就夠了；沒人動就當沒了，不留殭屍盤面）
MAX_AGE_S = 3 * 3600
#: 同時最多幾局（每個人各自算）：夠用就好，避免有人按著新局把記憶體塞滿
MAX_GAMES_PER_OWNER = 4


class GameError(Exception):
    """op 不合法／局不存在之類：呼叫端回 400 或 200+ok=false（照舊橋的習慣）。"""


class _Game:
    def __init__(self, gid: str, principal: str, entity: str, difficulty: str):
        self.id = gid
        self.principal = principal
        self.entity = entity
        self.difficulty = difficulty
        self.board = Board()
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.last: dict | None = None

    @property
    def winner(self) -> str:
        return str(getattr(self.board, "winner", "") or "")

    def frame(self, *, turn: str = "human", note: str = "") -> dict:
        # `Board.cells`：`{(x, y): "black"|"white"}`；客戶端只要這一份＋輪到誰
        stones = [[x, y, side] for (x, y), side in sorted(self.board.cells.items())]
        return {"id": self.id, "size": getattr(self.board, "size", 15), "stones": stones,
                "turn": turn, "winner": self.winner, "last": self.last or {},
                "difficulty": self.difficulty, "note": note,
                "entity": self.entity, "at": round(self.updated_at, 3)}


class GameStore:
    def __init__(self):
        self._games: dict[str, _Game] = {}
        self._lock = threading.RLock()

    # ── 對外 ───────────────────────────────────────────────────────────────
    def start(self, *, principal: str, entity: str, difficulty: str = "normal") -> dict:
        if not principal:
            raise GameError("principal required")
        with self._lock:
            self._sweep()
            mine = [g for g in self._games.values() if g.principal == principal]
            if len(mine) >= MAX_GAMES_PER_OWNER:
                oldest = sorted(mine, key=lambda g: g.updated_at)[0]
                self._games.pop(oldest.id, None)
            gid = "g" + uuid.uuid4().hex[:10]
            game = _Game(gid, principal, entity or "mira", difficulty or "normal")
            self._games[gid] = game
            return game.frame()

    def move(self, *, principal: str, gid: str, x: int, y: int) -> dict:
        with self._lock:
            game = self._get(principal, gid)
            if game.winner:
                return game.frame(turn="over", note="already_over")
            if not self._place(game, int(x), int(y), BLACK):
                return game.frame(turn="human", note="illegal")
            if game.winner:
                return game.frame(turn="over")
            # 她一手（引擎算，不經模型）
            move = choose_move(game.board, difficulty=game.difficulty)
            if move is not None:
                self._place(game, int(move[0]), int(move[1]), WHITE)
            return game.frame(turn="over" if game.winner else "human")

    def frame(self, *, principal: str, gid: str) -> dict:
        with self._lock:
            return self._get(principal, gid).frame()

    def close(self, *, principal: str, gid: str) -> dict:
        with self._lock:
            game = self._get(principal, gid)
            self._games.pop(game.id, None)
            return {"ok": True, "game": None}

    # ── 內部 ───────────────────────────────────────────────────────────────
    def _place(self, game: _Game, x: int, y: int, side: str) -> bool:
        board = game.board
        try:
            if not board.legal(x, y):
                return False
            board.play(x, y, side)
        except Exception:                       # noqa: BLE001 - 規則層不合法就當這手不算
            return False
        game.last = {"x": x, "y": y, "side": side}
        game.updated_at = time.time()
        return True

    def _get(self, principal: str, gid: str) -> _Game:
        game = self._games.get(str(gid))
        if game is None or game.principal != principal:
            raise GameError("no such game")
        return game

    def _sweep(self) -> None:
        now = time.time()
        for gid in [g.id for g in self._games.values() if now - g.updated_at > MAX_AGE_S]:
            self._games.pop(gid, None)
