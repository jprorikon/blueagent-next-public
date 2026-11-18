"""「遊戲中的那段上下文」——**獨立一段，不吃對話的滾動預算**。

owner 2026-10-22 的設計：「game 類的上下文在我們的滾動上下文裡面單獨佔一段，不被上下文限制
關係；遊戲結束後再回收這段。」

做法刻意壓到最小：

  · 遊戲（TRPG 桌）有變動時，由那一側 `set_block("trpg", …)` 寫進這裡（一份程序內快照）；
  · 拼 prompt 時（`brain/messages.py`）把這段**接在滾動段之後、本輪訊息之前**，
    而且**不從 budget 扣**（它有自己的上限 `MAX_CHARS`）；
  · 遊戲收掉（`forget_table`／`close`）就 `clear_block`，下一輪起這段就不見了 —— 回收。

為什麼是程序內全域：一個行程只服務一個她、最多一桌在玩；用全域比把 state 塞進 Envelope 乾淨
（Envelope 是純路由資料，不該順手多一個可寫的洞）。
"""

from __future__ import annotations

import threading

#: 這段自己的上限：不跟對話搶預算，但也不能無限大（她要看的是「這一局現在怎麼了」）
MAX_CHARS = 3000

_LOCK = threading.Lock()
_BLOCKS: dict[str, str] = {}


def set_block(kind: str, text: str) -> None:
    body = str(text or "").strip()
    with _LOCK:
        if body:
            _BLOCKS[str(kind)] = body[:MAX_CHARS]
        else:
            _BLOCKS.pop(str(kind), None)


def clear_block(kind: str) -> None:
    with _LOCK:
        _BLOCKS.pop(str(kind), None)


def clear_all() -> None:
    with _LOCK:
        _BLOCKS.clear()


def current_block() -> str:
    """給 prompt 用：目前所有進行中的遊戲段（空的就回 `""`）。"""
    with _LOCK:
        if not _BLOCKS:
            return ""
        parts = [f"[[{kind}]]\n{text}\n[[/{kind}]]" for kind, text in _BLOCKS.items()]
    body = "\n\n".join(parts)
    return body[:MAX_CHARS]


def snapshot() -> dict[str, str]:
    with _LOCK:
        return dict(_BLOCKS)
