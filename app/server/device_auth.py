"""人類帳號的 token 以 Agent 為準（多設備配對，owner 2026-11-16）。

為什麼：App Server 的帳號表是開機時讀的靜態檔（`agent/tools/provision_app_server.py` 產生），
只有每個帳號「第一台」的 token 雜湊。配對出來的新端末、被撤銷的端末，Agent 那邊立刻知道，
這裡要等下次重新產表、重啟才知道 ⇒ 新手機能叫 `/api/*` 卻打不開 `/v1/*`、丟掉的手機還能讀聊天。

做法：人類帳號（owner／guest）每把 token 都問 Agent 一次「這是誰」
（`POST <brain>/api/app/introspect`，帶既有的 `X-App-Kick-Token`，只送 SHA-256），答案快取 60 秒。
  · Agent 說有效 ⇒ 那個帳號；說無效 ⇒ 拒絕（就算本地靜態表還認得——那是被撤銷的）；
  · **Agent 連不上 ⇒ 看這把 token 上一次的結論**（審核 R1，owner 2026-11-17 定）：
      – 上次是「無效」⇒ 拒絕；
      – 上次是「有效」且在 `grace_s`（預設 10 分鐘）以內 ⇒ 照舊放行（Agent 短暫掉線，正在用的人不被踢掉）；
      – 從沒確認過、或上次確認已超過 `grace_s` ⇒ 拒絕。
    以前這裡一律退回靜態表 ⇒ 被撤銷的 primary 在 Agent 離線期間又能讀 `/v1/*`，而且沒有上限。
    ⇒ 撤銷的生效上限：Agent 在線＝快取 60 秒；Agent 離線＝距離上次確認 `grace_s`（再加 SSE 重驗 30 秒）。
    代價：App Server 重啟時 Agent 也不在 ⇒ 人類帳號要等 Agent 回來才進得去（結論只放記憶體）。
  · 沒設定 kick token（introspection 沒開）⇒ 照舊只看本地靜態表；
  · 角色（entity）帳號不問：那些 token 只在這裡。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

UNREACHABLE = object()

#: Agent 問不到時，上次確認「有效」還能撐多久（`BLUEAGENT_APP_AUTH_GRACE_S`）
DEFAULT_GRACE_S = 600.0
_LAST_MAX = 4096


class Introspector:
    def __init__(self, brain_url: str = "", token: str = "", *, ttl_s: float = 60.0,
                 timeout_s: float = 1.5, grace_s: float = DEFAULT_GRACE_S,
                 clock=time.monotonic, opener=None) -> None:
        self.url = (str(brain_url or "").rstrip("/") + "/api/app/introspect") if brain_url else ""
        self.token = str(token or "")
        self.ttl_s = float(ttl_s)
        self.timeout_s = float(timeout_s)
        self._clock = clock
        self._open = opener or urllib.request.urlopen
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, str]] = {}
        self.grace_s = max(0.0, float(grace_s))
        #: 每把 token 上一次問到的結論 `(問到的時刻, 帳號 id 或 "")`——Agent 掉線時靠它判斷
        self._last: dict[str, tuple[float, str]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def account_for(self, digest: str):
        """回帳號 id（有效）、空字串（無效／不放行）、或 `UNREACHABLE`（introspection 沒開，呼叫端看本地表）。

        Agent 問不到時不再回 `UNREACHABLE`，改按上一次的結論判（見模組說明）。
        """
        if not self.enabled:
            return UNREACHABLE
        now = float(self._clock())
        with self._lock:
            hit = self._cache.get(digest)
            if hit is not None and hit[0] > now:
                return hit[1]
        request = urllib.request.Request(
            self.url, data=json.dumps({"token_sha256": digest}).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "X-App-Kick-Token": self.token})
        try:
            with self._open(request, timeout=self.timeout_s) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except Exception:                               # noqa: BLE001 - Agent 掛了不能讓平台跟著掛
            return self._offline_verdict(digest, now)
        account = str(body.get("account_id") or "") if body.get("valid") else ""
        with self._lock:
            if len(self._cache) > 2048:
                self._cache.clear()
            self._cache[digest] = (now + self.ttl_s, account)
            if len(self._last) >= _LAST_MAX and digest not in self._last:
                oldest = min(self._last, key=lambda key: self._last[key][0])
                self._last.pop(oldest, None)
            self._last[digest] = (now, account)
        return account

    def _offline_verdict(self, digest: str, now: float) -> str:
        """Agent 問不到：上次無效 ⇒ 拒；上次有效且在寬限內 ⇒ 放行；其他（沒問過、太久）⇒ 拒。"""
        with self._lock:
            last = self._last.get(digest)
        if last is None:
            return ""
        verified_at, account = last
        if account and now - verified_at <= self.grace_s:
            return account
        return ""


__all__ = ["Introspector", "UNREACHABLE", "DEFAULT_GRACE_S"]
