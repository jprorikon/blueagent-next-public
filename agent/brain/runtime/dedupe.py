"""幂等窗口：**同一件事只处理一次**。

这是从另一份通用 agent spec 借来、但确实补上 blueagent 短板的一点：

  · TRPG 桌端每 10 秒重发一次 turn；断线重连会把同一批事件再灌一遍。
  · Discord/Unity 的 SSE 重连、语音碎片的重复提交。
  · Webhook/提醒的 at-least-once 投递。

旧模型靠「时间戳 + 去重表」散在各处（评审 §1-4 的 kick 重复执行就是没有统一去重）。
这里给一个无依赖、进程内、带上限与 TTL 的窗口；键由调用端拼成 `source:id`，
所以**不同源的同名 id 不会互相吞**。
"""

from __future__ import annotations

import threading
from collections import OrderedDict


class IdempotencyWindow:
    def __init__(self, *, ttl: float = 120.0, max_keys: int = 4096) -> None:
        self._ttl = max(0.0, float(ttl))
        self._max = max(16, int(max_keys))
        self._lock = threading.RLock()
        self._seen: OrderedDict[str, float] = OrderedDict()

    def seen(self, key: str, now: float) -> bool:
        """回「這個鍵在 TTL 內出現過嗎」。第一次出現會記下來（回 False）。"""
        token = str(key or "").strip()
        if not token:
            return False
        with self._lock:
            self._purge_locked(now)
            last = self._seen.get(token)
            if last is not None and now - last <= self._ttl:
                return True
            self._seen[token] = float(now)
            self._seen.move_to_end(token)
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return False

    def forget(self, key: str) -> bool:
        with self._lock:
            return self._seen.pop(str(key or "").strip(), None) is not None

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()

    def stats(self) -> dict:
        with self._lock:
            return {"keys": len(self._seen), "ttl_s": self._ttl, "max_keys": self._max}

    def _purge_locked(self, now: float) -> None:
        # 只清最舊的一小段：TTL 窗口下，絕大多數鍵都在頭部。
        while self._seen:
            key, at = next(iter(self._seen.items()))
            if now - at <= self._ttl:
                return
            self._seen.pop(key, None)
