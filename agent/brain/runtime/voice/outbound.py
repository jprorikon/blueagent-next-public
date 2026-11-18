"""她自己打電話給主人（owner 2026-11-17）：什麼時候可以打。

兩種時機，預設都開：

  · **聊天中她決定**：`voice.call(end=phone)`（`tools/voice.py`）。主人就在跟她說話，不另外擋頻率，
    只擋「她自己在別的通話裡」「主人那頭已經有一通」（App Server 回 `busy_elsewhere`）。
  · **自主想打**：心跳的內心獨白回 `action=call`（`CallWish` 包在 `Monologue` 外面）。這條要過閘：
      ① 主動消息的閘 `ProactiveGate.allows()`——暖機、她睡著、安靜時段、主人不方便（**通話中**／正在別處聊）、
        最小間隔、**每日上限**，全部沿用；打出去之後 `gate.mark()`，算進主動消息的每日上限。
      ② 電話自己的上限（`OutboundPolicy`）：一天最多 `BLUEAGENT_CALL_OUT_DAILY_MAX`（2）通、
        兩通之間至少 `BLUEAGENT_CALL_OUT_GAP_S`（3 小時）。電話比訊息吵，額度另算、更小。
    打不出去 ⇒ 那一拍退回 `talk`（她還是想找他，就改用說的）。

真正撥號的是 `VoiceService.dialer`（組合根接上 `adapter/app_call_out.AppCallDialer`；runtime 不 import adapter，
只認「有 `dial()`」的鴨子型別）。沒接撥號器 ⇒ `voice.call(end=phone)` 照舊是本機假通話。

`BLUEAGENT_CALL_OUT=0` ⇒ 兩種時機都關（`voice.call(end=phone)` 退回舊行為）；
`BLUEAGENT_CALL_OUT_SELF=0` ⇒ 只關自主那條。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time


def enabled(name: str = "BLUEAGENT_CALL_OUT") -> bool:
    return str(os.environ.get(name) or "1").strip().lower() not in {"0", "off", "false", "no"}


def _number(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name) or default))
    except ValueError:
        return float(default)


class OutboundPolicy:
    """電話自己的頻率閘（每個角色）：每日上限＋最小間隔；落盤，重啟不清零。"""

    def __init__(self, *, daily_max: int | None = None, gap_s: float | None = None,
                 state_path=None, clock=time.time) -> None:
        self.daily_max = int(_number("BLUEAGENT_CALL_OUT_DAILY_MAX", 2)
                             if daily_max is None else daily_max)
        self.gap_s = float(_number("BLUEAGENT_CALL_OUT_GAP_S", 3 * 3600) if gap_s is None else gap_s)
        self._clock = clock
        self._path = pathlib.Path(state_path) if state_path is not None else None
        self._lock = threading.Lock()
        #: entity → 最近幾次「她自己打出去」的時刻
        self._placed: dict[str, list[float]] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for entity, rows in (raw.get("placed") or {}).items() if isinstance(raw, dict) else ():
            try:
                self._placed[str(entity)] = [float(item) for item in rows][-10:]
            except (TypeError, ValueError):
                continue

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"placed": self._placed}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    def allows(self, entity: str, *, now: float | None = None) -> tuple[bool, str]:
        at = float(self._clock() if now is None else now)
        day = time.strftime("%Y-%m-%d", time.localtime(at))
        with self._lock:
            rows = self._placed.get(str(entity), [])
        if rows and self.gap_s and at - rows[-1] < self.gap_s:
            return False, "call_gap"
        today = sum(1 for item in rows if time.strftime("%Y-%m-%d", time.localtime(item)) == day)
        if self.daily_max >= 0 and today >= self.daily_max:
            return False, "call_daily_max"
        return True, "ok"

    def mark(self, entity: str, *, now: float | None = None) -> None:
        at = float(self._clock() if now is None else now)
        with self._lock:
            self._placed[str(entity)] = (self._placed.get(str(entity), []) + [at])[-10:]
            self._save_locked()


def self_call_allowed(entity: str, principal: str, *, gate, policy: OutboundPolicy,
                      now: float | None = None) -> tuple[bool, str]:
    """自主想打：先問主動消息的閘（安靜時段／她睡著／主人通話中／間隔／每日上限），再問電話自己的額度。"""
    if not enabled("BLUEAGENT_CALL_OUT_SELF"):
        return False, "self_call_off"
    if gate is not None:
        try:
            ok, reason = gate.allows(entity, now=now, principal=principal)
        except Exception as exc:                             # noqa: BLE001 - 閘壞了就不打
            return False, f"gate_error:{type(exc).__name__}"
        if not ok:
            return False, str(reason or "gate")
    return policy.allows(entity, now=now)


class CallWish:
    """包在 `Monologue` 外面：獨白回 `action=call` ⇒ 試著打電話；打出去了 ⇒ 這一拍不再開口（`none`），
    打不出去 ⇒ 退回 `talk`（獨白原文照舊只當動機，交給組句器）。其他屬性一律轉給原本的獨白。"""

    def __init__(self, inner, *, dial) -> None:
        #: `dial(entity, row) -> dict`（`{"ok": bool, "error": str}`）
        self._inner = inner
        self._dial = dial

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def beat(self, entity: str, *, facts: dict, at: float | None = None):
        row = self._inner.beat(entity, facts=facts, at=at)
        if not row or str(row.get("action") or "") != "call":
            return row
        try:
            result = self._dial(str(entity), dict(row)) or {}
        except Exception as exc:                             # noqa: BLE001 - 撥號壞了 ⇒ 改用說的
            result = {"ok": False, "error": f"error:{type(exc).__name__}"}
        row = dict(row)
        row["call"] = {"ok": bool(result.get("ok")), "error": str(result.get("error") or ""),
                       "call_id": str(result.get("call_id") or "")}
        row["action"] = "none" if result.get("ok") else "talk"
        return row


__all__ = ["CallWish", "OutboundPolicy", "enabled", "self_call_allowed"]
