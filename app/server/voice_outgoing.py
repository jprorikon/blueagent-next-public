"""她打給主人（owner 2026-11-17）：「響鈴中」的來電、接聽／拒接／逾時。

以前只有主人打給她（`POST /v1/calls`，她在 brain 決定接不接）。這裡是反方向：

  · 她（實體帳號，用自己的 token）`POST /v1/calls/outgoing {to}` ⇒ 建一通 `caller=entity`、
    狀態 `ringing` 的 `CallSession`，並在**主人的事件流**寫一條只給他看的 `call.incoming`
    （前台 App 彈全屏來電；Android 背景由 `NoticeListen` 讀同一條 SSE 彈系統通知）。
  · 主人 `POST /v1/calls/{id}/accept` ⇒ 先叫 brain `answer`（她那頭掛上 `VoiceSession`），成功才轉 `live`，
    之後音訊幀、通話事件流、掛斷全部走現有那一套。
  · 主人 `POST /v1/calls/{id}/decline`（或響鈴中掛斷）⇒ `declined`；**30 秒**沒人接 ⇒ `missed`；
    她自己在響鈴中掛掉 ⇒ `cancelled`。每次狀態變化另寫一條 `call.updated`（其他端收掉來電畫面／通知）。
  · 同一個帳號同時只能有一通（響鈴中也算），沿用 `busy_elsewhere`。

結果怎麼寫進她的行動日誌由 Agent 自己問 `/state`（`agent/adapter/app_call_out.py`），這裡不碰 Agent。
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import time

from app.server.events import append_event
from app.server.social.models import BoardError

#: 她打來時響多久沒人接就算 `missed`（秒）
OUTGOING_RING_S = 30.0
#: 響鈴中可見的「打來的理由」最多幾個字（只給 brain 的第一句用；畫面不顯示）
TOPIC_MAX = 120
#: 她發起那一通的終局狀態（Agent 看到這些就寫結果）
OUTGOING_FINAL = frozenset({"live", "ended", "declined", "missed", "cancelled", "failed"})


def ring_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("BLUEAGENT_CALL_OUT_RING_S") or OUTGOING_RING_S))
    except ValueError:
        return OUTGOING_RING_S


class CallNotices:
    """`call.incoming`／`call.updated` 寫進平台事件表（只給那個主人看）。

    事件表跟業務表同一個 SQLite；這裡用自己的連線、自己的事務。寫完立刻 `flush()`：
    `call.*` 在叫醒判據裡是「不叫醒任何 Agent」，flush 只是清掉這條執行緒的待叫佇列（背景計時器也會寫）。
    """

    def __init__(self, db_path, *, events=None, accounts=None) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5.0)
        self._db.execute("PRAGMA busy_timeout=5000")
        self.events = events
        self.accounts = accounts

    def __call__(self, account_id: str, event_type: str, payload: dict) -> None:
        with self._lock, self._db:
            append_event(self._db, event_type=event_type, at=time.time(),
                         actor_id=str(payload.get("entity") or ""), actor_kind="entity",
                         entity_kind="call", entity_id=str(payload.get("call_id") or ""),
                         payload=payload, audience=[str(account_id)])
        if self.events is not None:
            self.events.flush()

    def close(self) -> None:
        with self._lock:
            self._db.close()


class OutgoingCalls:
    """`AppVoiceService` 的混入：她發起的那一通。用到宿主的 `_sessions`／`_lock`／`bridge`／`_emit_locked`。"""

    #: `notify(account_id, event_type, payload)`；None＝不寫事件（單元測試）
    notify = None
    #: `missed(session)`：主人沒接（逾時／她響鈴中掛掉）時在私聊留「不在着信」；None＝不留（單元測試）
    missed = None

    def _record_missed(self, session) -> None:
        recorder = self.missed
        if recorder is None:
            return
        try:
            recorder(session)
        except Exception as exc:                          # noqa: BLE001 - 留不下紀錄不擋通話收尾
            self.log(f"app voice 不在着信沒留下：{session.id} {type(exc).__name__}")

    def _busy_locked(self, account_id: str) -> bool:
        """這個帳號是不是已經有一通（撥號中、響鈴中、接通中、通話中都算）。"""
        return account_id in self._dialing or any(
            item.account_id == account_id and item.state in {"ringing", "connecting", "live"}
            for item in self._sessions.values())

    def _visible(self, session, account) -> bool:
        """主人本人；或打出這一通的那個角色（她可以看狀態、在響鈴中掛掉）。"""
        if session.account_id == account.id:
            return True
        return account.kind == "entity" and session.caller == "entity" \
            and session.entity == account.id

    def _notice(self, session, event_type: str, **extra) -> None:
        notify = self.notify
        if notify is None:
            return
        display = ""
        target = self.accounts.get(session.entity)
        if target is not None:
            display = str(getattr(target, "display", "") or "")
        payload = {"call_id": session.id, "entity": session.entity, "display": display,
                   "caller": session.caller, "state": session.state,
                   "expires_at": round(session.expires_at, 3), **extra}
        try:
            notify(session.account_id, event_type, payload)
        except Exception as exc:                          # noqa: BLE001 - 通知寫不進去不擋通話
            self.log(f"app voice 來電通知沒寫進去：{session.id} {type(exc).__name__}")

    # ── 她打出去 ────────────────────────────────────────────────────────────
    def dial(self, caller, to: str, topic: str = "", language: str = "") -> dict:
        from app.server.voice import CallSession     # 延遲 import：voice.py 也 import 這支

        self._purge()
        if caller.kind != "entity":
            raise BoardError(403, "forbidden")
        owner = self.accounts.get(str(to or ""))
        if owner is None or owner.kind != "owner":
            raise BoardError(404, "owner_not_found")
        if owner.visible_entities is not None and caller.id not in owner.visible_entities:
            raise BoardError(403, "call_entity_forbidden")
        ring_s = float(getattr(self, "out_ring_s", 0.0) or ring_seconds())
        session = CallSession("call_" + secrets.token_hex(12), owner.id,
                              owner.legacy_principal or owner.id, caller.id,
                              str(language or "")[:16])
        session.state, session.caller = "ringing", "entity"
        session.topic = " ".join(str(topic or "").split())[:TOPIC_MAX]
        session.expires_at = time.time() + ring_s
        with self._lock:
            if self._busy_locked(owner.id):
                raise BoardError(409, "busy_elsewhere")
            self._sessions[session.id] = session
        self.log(f"app voice 她打給主人：{session.id} {caller.id} → {owner.id}")
        self._notice(session, "call.incoming", ring_s=ring_s)
        timer = threading.Timer(ring_s, self._ring_out_expired, args=(session,))
        timer.daemon = True
        timer.start()
        return {"session": session.public(), "state": "ringing",
                "expires_at": round(session.expires_at, 3)}

    def incoming(self, account) -> dict:
        """主人這邊此刻響著的那一通（App 回到前台時問；沒有 ⇒ `session=None`）。"""
        self._purge()
        with self._lock:
            found = [item for item in self._sessions.values()
                     if item.account_id == account.id and item.caller == "entity"
                     and item.state == "ringing" and item.expires_at > time.time()]
        if not found:
            return {"session": None}
        session = found[0]
        return {"session": session.public(), "state": session.state,
                "expires_at": round(session.expires_at, 3)}

    def _ring_out_expired(self, session) -> None:
        with session.lock:
            if session.state != "ringing":
                return
            self._finish_ring_locked(session, "missed", "timeout")
        self.log(f"app voice 她打的沒人接：{session.id}")
        self._notice(session, "call.updated", reason="timeout")
        self._record_missed(session)

    def _finish_ring_locked(self, session, state: str, reason: str) -> None:
        session.state, session.pickup, session.reason = state, state, reason
        session.ended_at = time.time()
        self._emit_locked(session, "call", {**session.public(), "reason": reason})

    def _ringing_out(self, account, call_id: str):
        session = self._sessions.get(str(call_id))
        if session is None or session.account_id != account.id or session.caller != "entity":
            raise BoardError(404, "call_not_found")
        return session

    # ── 主人這頭 ────────────────────────────────────────────────────────────
    def decline(self, account, call_id: str) -> dict:
        session = self._ringing_out(account, call_id)
        with session.lock:
            if session.state != "ringing":
                raise BoardError(410, "call_ended", state=session.state)
            self._finish_ring_locked(session, "declined", "declined")
        self.log(f"app voice 主人拒接：{session.id}")
        self._notice(session, "call.updated", reason="declined")
        return {"ok": True, "session": session.public(), "state": session.state}

    def accept(self, account, call_id: str) -> dict:
        session = self._ringing_out(account, call_id)
        with session.lock:
            if session.state != "ringing":
                raise BoardError(410, "call_ended", state=session.state)
            session.state = "connecting"                # 擋住逾時計時器與第二次接聽
        try:
            self._answer_brain(session)
        except BoardError as exc:
            with session.lock:
                self._finish_ring_locked(session, "failed", exc.code)
            self.log(f"app voice 接聽失敗：{session.id} {exc.code}")
            self._release_brain(session, exc.code)
            self._notice(session, "call.updated", reason=exc.code)
            raise
        with session.lock:
            session.state, session.pickup = "live", "accepted"
            session.last_seen = time.time()
            self._emit_locked(session, "call", {**session.public(), "pickup": "accepted",
                                                "answered": True})
        threading.Thread(target=self._run, args=(session,), daemon=True,
                         name="app-voice-" + session.id[-6:]).start()
        self._notice(session, "call.updated", reason="accepted")
        return {"session": session.public(), "state": "live", "audio": self.audio_format()}

    def _release_brain(self, session, cause: str) -> None:
        """接聽沒成功：照同一個 call_id 叫 brain 收掉她那頭（審核 OC-3）。

        brain 可能其實已經掛上了、只是回應在路上丟了（逾時／連線斷）——平台這邊不知道，就主動對帳，
        不然她那頭一直「在通話中」，下一通會被當成忙。brain 的 `hangup` 帶 call_id 只收這一通，
        不會誤掛她正在講的別通。收不掉只記日誌，原本的接聽錯誤照樣回給主人。
        """
        try:
            self.bridge.call("hangup", self._payload(session))
        except BoardError as exc:
            self.log(f"app voice 接聽失敗後收 brain 那頭也失敗：{session.id} {cause} → {exc.code}")

    def _answer_brain(self, session) -> None:
        """叫 brain 掛上她那頭的通話。舊 brain（不認得 `answer`）退回 `start`。"""
        payload = {**self._payload(session), "caller": "entity", "topic": session.topic}
        try:
            reply = self.bridge.call("answer", payload)
        except BoardError as exc:
            if exc.status != 400 or exc.code != "bad_voice_action":
                raise
            reply = self.bridge.call("start", self._payload(session))
        if isinstance(reply, dict) and reply.get("ok") is False:
            raise BoardError(409, str(reply.get("error") or "call_failed"))

    def cancel_out(self, session) -> dict:
        """她自己在響鈴中掛掉（實體帳號的 hangup）。"""
        with session.lock:
            if session.state != "ringing":
                raise BoardError(410, "call_ended", state=session.state)
            self._finish_ring_locked(session, "cancelled", "caller_cancelled")
        self._notice(session, "call.updated", reason="caller_cancelled")
        self._record_missed(session)
        return {"ok": True, "session": session.public(), "state": session.state}


__all__ = ["CallNotices", "OutgoingCalls", "OUTGOING_FINAL", "OUTGOING_RING_S", "ring_seconds"]
