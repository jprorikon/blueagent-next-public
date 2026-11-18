"""她打給主人：經 App Server 讓主人的 App 響鈴，等結果，寫進她的私人行動日誌（owner 2026-11-17）。

  · `dial()`：用**她自己的帳號** `POST /v1/calls/outgoing`（App Server 建響鈴中的來電、推 `call.incoming`）。
    先擋：她自己在別的通話裡（`busy_self`）、同一個她已經在響（回同一通）、
    自主想打要過閘（`runtime/voice/outbound.self_call_allowed`）；主人那頭已經有一通 ⇒ `owner_busy`。
  · 背景盯 `/v1/calls/{id}/state` 到終局：
      - 接了（`live`）⇒ 什麼都不記（通話本身會進她的對話線；她那頭的 session 由 App Server 叫 `answer` 掛上，
        第一句帶 `ANSWERED_NOTE`，她知道是自己打的）。
      - 拒接（`declined`）／沒人接（`missed`，App Server 30 秒逾時）／沒接通（`failed`）
        ⇒ 行動日誌記一筆 `call_unanswered`（方向跟 `call_missed` 相反）。
  · 要不要補一句私聊**由她決定**：撥號時給了 `if_unanswered` 才送（主動私聊，同 `app_call_decline`）。
    聊天中打＝`voice.call` 的參數；自主想打＝獨白 JSON 的 `if_unanswered`（2026-11-17 owner 定，`self_dial` 轉過來）。
    **日誌記的是實際結果**（審核 R3「送完才寫」）：一筆、在送完之後寫——送到 ⇒「…と送った」；
    沒送到 ⇒「送ろうとしたが届かなかった」＋原因。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from agent.adapter.app_call_decline import NOT_SENT, SENT, _send
from agent.brain.runtime.voice.outbound import OutboundPolicy, self_call_allowed

#: 主人接了她打的電話：第一句的 `[[room]]`（`app_voice._answer`）
ANSWERED_NOTE = "（自分からオーナーに電話をかけて、いまオーナーが出てくれたところ。{why}）"
NOTES = {
    "declined": "オーナーに電話をかけたけど、出てもらえなかった（切られた）",
    "missed": "オーナーに電話をかけたけど、出なかった（しばらく鳴らしても応答なし）",
    "failed": "オーナーに電話をかけようとしたが、つながらなかった",
    "cancelled": "オーナーに電話をかけたけど、出てもらう前に自分で切った",
    "unknown": "オーナーに電話をかけたけど、どうなったのか分からない",
}
#: 平台回的終局 → 行動日誌的結果（審核 OC-4：以前只認 declined／missed，其他一律寫成「沒連上」）
OUTCOMES = {"declined": "declined", "missed": "missed", "cancelled": "cancelled", "failed": "failed"}
#: 響鈴中的狀態（還沒結果）
RINGING = frozenset({"ringing", "connecting"})


def _code(exc: Exception) -> str:
    text = str(exc)
    return text[len("app_server_http_"):] if text.startswith("app_server_http_") else \
        f"error:{type(exc).__name__}"


def _failed(code: str) -> dict:
    return {"ok": False, "error": code, "frames": [
        {"kind": "call", "payload": {"state": "failed", "end": "phone", "reason": code}}]}


@dataclass
class Pending:
    entity: str
    principal: str
    call_id: str
    reason: str = ""
    if_unanswered: str = ""
    origin: str = "chat"
    started: float = field(default_factory=time.time)


class AppCallDialer:
    """`VoiceService.dialer` 的實作。`clients`：`{entity: AppServerClient}`（每個角色用自己的 token）。"""

    def __init__(self, app, clients: dict, *, accounts=None, policy: OutboundPolicy | None = None,
                 log=None, poll_s: float = 1.0, watch_s: float = 45.0) -> None:
        self.app = app
        self.clients = dict(clients or {})
        self.accounts = accounts
        self.policy = policy or OutboundPolicy()
        self._log = log or (lambda message: print(f"  {message}", flush=True))
        self.poll_s = float(poll_s)
        #: 盯結果最多多久（App Server 響 30 秒；多留一點給網路）
        self.watch_s = float(watch_s)
        self._lock = threading.Lock()
        self._ringing: dict[str, Pending] = {}
        self._threads: dict[str, threading.Thread] = {}

    def _account(self, principal: str) -> str:
        if self.accounts is None:
            return principal
        try:
            return str(self.accounts.account_id_of(principal) or principal)
        except Exception:                                  # noqa: BLE001 - 翻不了就原樣用
            return principal

    def pending(self, call_id: str) -> Pending | None:
        with self._lock:
            return next((item for item in self._ringing.values() if item.call_id == str(call_id)), None)

    # ── 撥號 ────────────────────────────────────────────────────────────────
    def dial(self, entity: str, *, principal: str = "owner", reason: str = "",
             if_unanswered: str = "", origin: str = "chat") -> dict:
        client = self.clients.get(str(entity))
        if client is None:
            return _failed("end_unavailable:phone")
        voice = getattr(self.app, "voice", None)
        if voice is not None and voice.active(entity) is not None:
            return _failed("busy_self")                    # 她自己在別的通話裡
        with self._lock:
            ringing = self._ringing.get(str(entity))
        if ringing is not None:
            return {"ok": True, "existing": True, "state": "ringing", "call_id": ringing.call_id,
                    "frames": []}
        gate = getattr(self.app, "proactive", None)
        if origin == "self":
            ok, why = self_call_allowed(entity, principal, gate=gate, policy=self.policy)
            if not ok:
                self._log(f"call out: {entity} 想打但沒打（{why}）")
                return _failed(why)
        try:
            reply = client._request("POST", "/v1/calls/outgoing", {
                "to": self._account(principal), "topic": str(reason or "")[:120]})
        except Exception as exc:                          # noqa: BLE001
            code = _code(exc)
            self._log(f"call out: {entity} 撥不出去（{code}）")
            return _failed("owner_busy" if code == "busy_elsewhere" else code)
        call_id = str(((reply or {}).get("session") or {}).get("id") or "")
        if not call_id:
            return _failed("bad_reply")
        self.policy.mark(entity)
        if origin == "self" and gate is not None and hasattr(gate, "mark"):
            gate.mark(entity, principal=principal)        # 算進主動消息的間隔與每日上限
        pending = Pending(str(entity), str(principal), call_id, str(reason or ""),
                          " ".join(str(if_unanswered or "").split())[:80], str(origin or "chat"))
        worker = threading.Thread(target=self._watch, args=(client, pending), daemon=True,
                                  name="call-out-" + call_id[-6:])
        with self._lock:
            self._ringing[pending.entity] = pending
            self._threads[call_id] = worker
        worker.start()
        self._log(f"call out: {entity} 打給 {principal}（{origin}，{call_id}）")
        return {"ok": True, "existing": False, "state": "ringing", "call_id": call_id,
                "frames": [{"kind": "call", "payload": {
                    "state": "ringing", "end": "phone", "call_id": call_id, "entity": str(entity)}}]}

    def join(self, call_id: str, timeout: float = 5.0) -> None:
        """測試用：等背景收尾跑完。"""
        with self._lock:
            worker = self._threads.get(str(call_id))
        if worker is not None:
            worker.join(timeout)

    # ── 盯結果 ──────────────────────────────────────────────────────────────
    def _watch(self, client, pending: Pending) -> None:
        deadline = time.monotonic() + self.watch_s
        state, reason = "ringing", ""
        while time.monotonic() < deadline:
            try:
                body = client._request("GET", f"/v1/calls/{pending.call_id}/state") or {}
            except Exception as exc:                      # noqa: BLE001
                if _code(exc) == "404":
                    state, reason = "failed", "call_lost"
                    break
                body = {"state": "ringing"}               # 一時連不上：下一輪再問
            state, reason = str(body.get("state") or "ringing"), str(body.get("reason") or "")
            if state not in RINGING:
                break
            time.sleep(self.poll_s)
        if state in RINGING:
            # 平台逾時沒生效（或一直問不到）：自己收掉，照「沒人接」記
            try:
                client._request("POST", f"/v1/calls/{pending.call_id}/hangup", {})
            except Exception:                             # noqa: BLE001
                pass
            state, reason = "missed", "watch_timeout"
        try:
            self._finish(pending, state, reason)
        finally:
            with self._lock:
                if self._ringing.get(pending.entity) is pending:
                    self._ringing.pop(pending.entity, None)
                self._threads.pop(pending.call_id, None)

    def _finish(self, pending: Pending, state: str, reason: str) -> None:
        if state in {"live", "ended"}:
            self._log(f"call out: {pending.entity} 打的電話主人接了（{pending.call_id}）")
            return
        # 她自己掛掉＝cancelled（不是「沒連上」）；認不得的狀態記 unknown＋原始 state，不偽裝成 failed
        outcome = OUTCOMES.get(state, "unknown")
        text = NOTES[outcome]
        extra = {"outcome": outcome, "reason": reason, "call_id": pending.call_id,
                 "origin": pending.origin, "principal": pending.principal}
        if outcome == "unknown":
            extra["state"] = state
        line = pending.if_unanswered
        if line and outcome in {"declined", "missed"}:            # 自己掛的、沒連上的不留言
            delivered, failure = _send(self.app, pending.entity, pending.principal, line)
            text += (SENT if delivered else NOT_SENT).format(line=line)
            extra["delivered"] = delivered
            if not delivered:
                extra["failure"] = failure
        self._log(f"call out: {pending.entity} 打的電話沒接通（{outcome}／{reason or '-'}）")
        log = getattr(self.app, "activity_log", None)
        if log is None:
            return
        try:
            log.append(pending.entity, kind="call_unanswered", text=text, extra=extra)
        except Exception as exc:                          # noqa: BLE001 - 記不上不影響別的
            self._log(f"call out: 行動日誌沒記上（{type(exc).__name__}）")


def attach(app, adapters, *, log=None, state_path=None) -> AppCallDialer | None:
    """把撥號器接到 `app.voice.dialer`（沒有 App 帳號 ⇒ 不接，`voice.call(end=phone)` 照舊）。"""
    voice = getattr(app, "voice", None)
    clients = {adapter.entity: adapter.client for adapter in (adapters or ())}
    if voice is None or not clients:
        return None
    old = getattr(voice, "dialer", None)
    policy = getattr(old, "policy", None) or OutboundPolicy(state_path=state_path)
    dialer = AppCallDialer(app, clients, policy=policy,
                           accounts=getattr(app, "identity", None) or getattr(app, "accounts", None),
                           log=(lambda message: log(message)) if log is not None else None)
    voice.dialer = dialer
    return dialer


def self_dial(voice, principal: str = "owner"):
    """給 `CallWish`：自主想打 ⇒ `dial(origin="self")`；沒接撥號器 ⇒ 打不出去（退回說的）。

    撥號器是 App adapter 起來之後才接上的（`compose/state.py`），所以每次都現查 `voice.dialer`。
    """

    def dial(entity: str, row: dict) -> dict:
        dialer = getattr(voice, "dialer", None)
        if dialer is None:
            return {"ok": False, "error": "no_dialer"}
        why = str(row.get("why") or "").strip()
        # 獨白自己寫了「沒接通就送的一句」才送（owner 2026-11-17：自主打的也可以留言，送不送由她）
        return dialer.dial(entity, principal=principal, reason=why, origin="self",
                           if_unanswered=str(row.get("if_unanswered") or "").strip())

    return dial


__all__ = ["ANSWERED_NOTE", "AppCallDialer", "NOTES", "Pending", "attach", "self_dial"]
