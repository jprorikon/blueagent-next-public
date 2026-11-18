"""送出請求時的「還沒出字就重試」（owner 2026-10-30 起；2026-11-03 從 `openai_compat` 拆出來）。

為什麼只在這一步重試：HTTP 狀態碼只會在 `urlopen` 這一步拋出，這時一個字都還沒流出去、
工具也還沒跑，重送不會讓她把話說兩遍。串流開始之後的錯誤、網路斷線一律照舊直接失敗。

等多久：有 `Retry-After` 就照它，沒有就 3／6／12 秒；要等超過 30 秒、
或等完就過了這一輪的 deadline ⇒ 不等了，直接失敗（並發高時的 429 是主要對象）。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

#: 值得重送的狀態碼（其餘照舊直接失敗）
RETRY_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
#: 沒有 `Retry-After` 時依序等多久（秒）
RETRY_DELAYS: tuple[float, ...] = (3.0, 6.0, 12.0)
#: 等超過這個秒數就不等了（等下去還不如直接失敗）
RETRY_AFTER_MAX_S = 30.0


def retry_after(exc: urllib.error.HTTPError) -> float | None:
    """`Retry-After` 標頭（秒）；沒有或看不懂就回 None。"""
    try:
        raw = exc.headers.get("Retry-After") if exc.headers else None
    except AttributeError:
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def open_with_retry(request, req, timeout: float, *, sleep=time.sleep, safe_body=None):
    """送出請求；還沒任何輸出時遇到 408／429／5xx 就等一下再送（產生器，回 response）。

    `safe_body(exc)` 用來在日誌裡顯示後端回的錯誤內容（呼叫端提供，避免這裡依賴它）。
    """
    from agent.brain.stream_guard import stream_failure  # 避免循環匯入

    started = time.monotonic()
    deadline_s = request.deadline_ms / 1000.0 if request.deadline_ms else 0.0
    attempt = 0
    while True:
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS or attempt >= len(RETRY_DELAYS):
                raise
            wait = retry_after(exc)
            if wait is None:
                wait = RETRY_DELAYS[attempt]
            elapsed = time.monotonic() - started
            if wait > RETRY_AFTER_MAX_S or (deadline_s and elapsed + wait >= deadline_s):
                raise
            attempt += 1
            detail = safe_body(exc) if safe_body else ""
            print(f"  brain[{request.entity}] HTTP {exc.code}，{wait:.1f} 秒後重送"
                  f"（第 {attempt} 次）：{detail[:120]!r}", flush=True)
            yield ("retry", {"reason": f"http_{exc.code}", "attempt": attempt,
                             "wait_s": round(wait, 2)})
            sleep(wait)
            if deadline_s:
                timeout = max(1.0, min(timeout, deadline_s - (time.monotonic() - started)))


__all__ = ["open_with_retry", "retry_after", "RETRY_STATUS", "RETRY_DELAYS",
           "RETRY_AFTER_MAX_S", "stream_failure"]
