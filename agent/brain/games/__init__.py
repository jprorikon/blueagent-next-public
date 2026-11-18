"""把舊橋的五子棋規則層／引擎搬進新倉（`ba/game/gomoku_board.py` ＋ `gomoku_engine.py`）。

**只搬純邏輯**（零依賴、stdlib）：規則與棋力留在伺服器一處，客戶端只負責畫。
舊橋那一層的對外檔案（`gomoku.py` 的 `GAMES` 管理器、`bridge.py` 的 HTTP 路由）
跟著 `ba.core` 的依賴走，這輪先不搬；新倉自己用 `brain/games/session.py`
管「一局」的生命週期（用完即棄、與會話無關）。
"""

from __future__ import annotations
