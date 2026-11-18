"""SSE 事件名的兩條規則（從 `server/http.py` 抽出來，那個檔有 600 行棘輪）。

  ① 一輪串流的幀：`frame` 用內層 `kind` 當事件名（App 直接 switch 事件名）；
  ② 常駐推播：聊天交付一律叫 `delivery`，`mood` 是獨立事件（不能進聊天訊息處理）。
"""

from __future__ import annotations


def sse_name(kind: str, payload) -> tuple[str, dict]:
    """`frame` ⇒ 用內層 `kind` 當事件名。"""
    if kind == "frame" and isinstance(payload, dict):
        return str(payload.get("kind") or "frame"), payload
    return str(kind), payload


def bus_event_name(envelope) -> str:
    """聊天交付維持 `delivery`；mood 是獨立事件。"""
    return "mood" if isinstance(envelope, dict) and envelope.get("kind") == "mood" else "delivery"


__all__ = ["sse_name", "bus_event_name"]
