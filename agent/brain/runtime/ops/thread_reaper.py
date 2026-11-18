"""線的回收器（E4，2026-11-17）：定期把不會再用的線關掉，片段尾巴補封片。

為什麼要有：`ThreadStore.reap()` 從第一版起就沒有人呼叫。每次過窗（INTERACTIVE 15 分鐘）
`attach()` 開新世代，舊世代就留在 store 裡，直到 `max_threads`（1024）才被擠掉——
線上實測 513 條活線、`threads.json` 2.48 MB、每輪 persist p95 31 ms，而且只會線性變大。

規則在 `ThreadStore.prune()`（舊世代 6 小時、當世代 30 天、跑著的不關）；這裡只負責：
  · 定期呼叫（開機先跑一次，把累積的舊線一次清掉）；
  · 被關掉的線若還有沒封片的尾巴（換世代時舊線的最後一段從來沒封過），**最近一天內的**補封一次，
    結束時刻用那條線最後活動的時間，不用「現在」——封片檔是時間軸，不能把舊事記成剛發生；
    更舊的不補（原文倉已經有它們；一次補幾百段會把封片檔的「最近」灌滿舊東西）。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from agent.brain.domain.episode import CutReason

#: 被關掉的線，最後活動在這麼久以內才補封片。
SEAL_WITHIN_S = 24 * 3600.0


class ThreadReaper:
    def __init__(self, threads, *, seal: Callable | None = None, log: Callable[[str], None] | None = None,
                 superseded_after: float | None = None, stale_after: float | None = None,
                 seal_within: float = SEAL_WITHIN_S) -> None:
        self.threads = threads
        self.seal = seal
        self.log = log
        #: None＝用 `ThreadStore.prune()` 自己的預設（門檻定義在那裡，這裡不重抄一份）
        self.thresholds = {name: float(value) for name, value in
                           (("superseded_after", superseded_after), ("stale_after", stale_after))
                           if value is not None}
        self.seal_within = float(seal_within)
        self.last_result: dict = {}

    def run_once(self, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        closed = self.threads.prune(now, **self.thresholds)
        sealed = 0
        for thread in closed:
            if self.seal is None or not thread.segment:
                continue
            ended = float(thread.last_active_at or 0.0)
            if now - ended > self.seal_within:
                continue
            try:
                if self.seal(thread, ended, CutReason.IDLE) is not None:
                    sealed += 1
            except Exception:                      # noqa: BLE001 - 封片失敗不擋回收
                continue
        result = {"closed": len(closed), "sealed": sealed, "alive": len(self.threads), "at": round(now, 3)}
        self.last_result = result
        if closed and self.log is not None:
            self.log(f"thread-reaper: closed={len(closed)} sealed={sealed} alive={result['alive']}")
        return result


def start_worker(reaper: ThreadReaper, *, interval_s: float = 600.0) -> threading.Thread:
    """背景執行緒：開機先跑一次，之後每 `interval_s` 一次。"""
    def loop() -> None:
        while True:
            try:
                reaper.run_once()
            except Exception as exc:               # noqa: BLE001
                reaper.last_result = {"error": f"{type(exc).__name__}: {exc}"}
            time.sleep(max(60.0, float(interval_s)))

    thread = threading.Thread(target=loop, name="thread-reaper", daemon=True)
    thread.start()
    return thread
