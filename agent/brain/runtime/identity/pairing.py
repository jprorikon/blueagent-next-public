"""配對碼：已登入的端末幫同一個帳號簽一台新端末（owner 2026-11-16：二維碼＋6 位數字兩種都要）。

流程：
  ① 舊端末（已登入，主人或客人都行）`start(account_id)` ⇒ 6 位數字，5 分鐘有效、只能用一次；
     二維碼內容是 `<伺服器>/pair/<碼>`（系統相機掃了開落地頁，落地頁一顆鈕跳回 App）。
  ② 新端末 `claim(碼)` ⇒ 回帳號 id；呼叫端（`server/api/pairing.py`）接著簽一把新端末 token。

只放記憶體：重啟就作廢（5 分鐘的東西不值得落盤，也少一個要保護的檔）。
猜碼的節流在呼叫端（按 IP，`AuthService.allow`）；這裡另外限制「同一個碼最多錯幾次」沒有意義
（猜的人不知道碼），改成**同一帳號同時只留一個有效碼**——再按一次「加端末」舊碼就作廢。
"""

from __future__ import annotations

import secrets
import threading
import time

TTL_S = 300.0
DIGITS = 6


class PairingStore:
    def __init__(self, *, clock=time.time, ttl_s: float = TTL_S) -> None:
        self._clock = clock
        self.ttl_s = float(ttl_s)
        self._lock = threading.Lock()
        self._codes: dict[str, dict] = {}

    def _sweep_locked(self, now: float) -> None:
        for code in [code for code, item in self._codes.items() if item["expires_at"] <= now]:
            self._codes.pop(code, None)

    def start(self, account_id: str) -> dict:
        """回 `{code, expires_at}`；同一帳號之前的碼作廢。"""
        now = float(self._clock())
        with self._lock:
            self._sweep_locked(now)
            for code in [code for code, item in self._codes.items()
                         if item["account_id"] == str(account_id)]:
                self._codes.pop(code, None)
            code = ""
            for _ in range(50):
                code = f"{secrets.randbelow(10 ** DIGITS):0{DIGITS}d}"
                if code not in self._codes:
                    break
            item = {"account_id": str(account_id), "expires_at": now + self.ttl_s}
            self._codes[code] = item
            return {"code": code, "expires_at": item["expires_at"]}

    def claim(self, code: str) -> str:
        """碼 ⇒ 帳號 id（用掉就沒了）；不對或過期回空字串。"""
        key = "".join(ch for ch in str(code or "") if ch.isdigit())
        if len(key) != DIGITS:
            return ""
        now = float(self._clock())
        with self._lock:
            self._sweep_locked(now)
            item = self._codes.pop(key, None)
        return str(item["account_id"]) if item else ""

    def pending(self, code: str) -> bool:
        """落地頁用：這個碼現在還有效嗎（不消耗）。"""
        now = float(self._clock())
        with self._lock:
            self._sweep_locked(now)
            return str(code or "") in self._codes


__all__ = ["DIGITS", "PairingStore", "TTL_S"]
