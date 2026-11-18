"""平台实时通话：PCM 小帧接入、服务端切句、brain pipe 与会话内事件流。

这里不 import Agent，也不使用社交 EventLog。原始帧、切句缓冲和输出事件都只留在内存；
App Server 不执行 STT，完整 utterance 才交给 brain。语音消息的持久化路径与此无关。
开通话前先向 brain 发 `ring`：她决定接了才建会话（不接 ⇒ 409 `call_declined`，见 `AppVoiceService._ring`）。
反方向（她打給主人：響鈴、接聽／拒接／逾時）在 `voice_outgoing.py`。
"""

from __future__ import annotations

import audioop
import base64
import io
import json
import math
import queue
import secrets
import threading
import time
import urllib.error
import urllib.request
import wave
from collections import deque
from dataclasses import dataclass, field

from app.server.social.models import BoardError
from app.server.voice_outgoing import OutgoingCalls

RATE = 16000
FRAME_SAMPLES = RATE // 50
FRAME_BYTES = FRAME_SAMPLES * 2
MAX_FRAME_BYTES = FRAME_BYTES * 25       # 最多 500 ms；正常客户端每 100 ms 一帧
MAX_UTTERANCE_BYTES = RATE * 2 * 16
IDLE_S = 45.0
ENDED_TTL_S = 60.0
EVENT_LIMIT = 512
#: 響鈴（`ring`）等 brain 回話的上限（秒）
RING_TIMEOUT_S = 15.0
#: 她不接時 App 拿到的理由（`call_declined` 的 `reason`）；其他值一律歸成 `declined`
DECLINE_REASONS = {"asleep", "on_call", "declined"}


def pcm16_wav(pcm: bytes) -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(RATE)
        clip.writeframes(pcm)
    return target.getvalue()


class EnergyCutter:
    """与旧 Unity CallVad 相同的 20 ms 能量切句；输入为 16 kHz mono s16le。"""

    def __init__(self) -> None:
        self.start_db = -42.0
        self.noise_margin_db = 12.0
        self.hold_drop_db = 4.0
        self.start_ms = 160
        # 2026-11-08 owner 目標「像打電話」：靜音判定 700 → 450 ms。
        # 700 ms 只是保守（怕把他的停頓切成兩句），代價是每句都要先等 0.7 秒；
        # 450 ms 配上 preroll 300 ms 仍不會切斷正常語流，但反應快 0.25 秒。
        self.end_ms = 450
        self.min_speech_ms = 300
        self.max_speech_ms = 15000
        self.preroll_ms = 300
        self.noise_db = -60.0
        self.level_db = -90.0
        self.speaking = False
        self._pending = bytearray()
        self._recent = deque()
        self._utterance = []
        self._voiced_run = 0
        self._silence_run = 0
        self._voiced_total = 0

    @property
    def threshold(self) -> float:
        return max(self.start_db, self.noise_db + self.noise_margin_db)

    def reset(self) -> None:
        self.speaking = False
        self._pending.clear()
        self._recent.clear()
        self._utterance.clear()
        self._voiced_run = self._silence_run = self._voiced_total = 0

    def feed(self, pcm: bytes) -> list[tuple[str, bytes]]:
        self._pending.extend(pcm)
        events = []
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            events.extend(self._step(frame))
        return events

    def _step(self, frame: bytes) -> list[tuple[str, bytes]]:
        rms = audioop.rms(frame, 2)
        db = -90.0 if rms < 1 else 20.0 * math.log10(rms / 32768.0)
        self.level_db = db
        if not self.speaking:
            rate = 0.2 if db < self.noise_db else 0.02
            self.noise_db = max(-80.0, min(-30.0, self.noise_db + (db - self.noise_db) * rate))
            self._recent.append(frame)
            keep = (self.preroll_ms + self.start_ms) // 20 + 1
            while len(self._recent) > keep:
                self._recent.popleft()
            self._voiced_run = self._voiced_run + 20 if db >= self.threshold else 0
            if self._voiced_run < self.start_ms:
                return []
            self.speaking = True
            self._utterance = list(self._recent)
            self._recent.clear()
            self._voiced_total = self._voiced_run
            self._silence_run = 0
            return [("started", b"")]

        self._utterance.append(frame)
        if db >= self.threshold - self.hold_drop_db:
            self._silence_run = 0
            self._voiced_total += 20
        else:
            self._silence_run += 20
        length = len(self._utterance) * 20
        if self._silence_run >= self.end_ms:
            trailing = max(0, (self._silence_run - 200) // 20)
            return self._finish(self._voiced_total >= self.min_speech_ms, trailing)
        if length >= self.max_speech_ms:
            return self._finish(True, 0)
        return []

    def _finish(self, keep: bool, drop_tail: int) -> list[tuple[str, bytes]]:
        frames = max(0, len(self._utterance) - drop_tail)
        pcm = b"".join(self._utterance[:frames]) if keep else b""
        self.speaking = False
        self._utterance = []
        self._voiced_run = self._silence_run = self._voiced_total = 0
        return [("utterance" if keep else "dropped", pcm)]


class BrainVoiceBridge:
    """App Server → brain 的窄 HTTP pipe；共享密钥不属于任何平台用户。"""

    def __init__(self, base_url: str, token: str, *, timeout: float = 180.0) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def call(self, action: str, payload: dict) -> dict:
        if not self.configured:
            raise BoardError(503, "voice_bridge_unconfigured")
        body = json.dumps({"action": action, **payload}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/internal/app/voice", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-App-Voice-Token": self.token})
        # 響鈴（她決定接不接）有上限：brain 那頭判斷最多約 3 秒，這裡給 15 秒就算 brain 卡住也不會讓
        # 主人一直停在「呼び出し中」；一般回合（STT＋對話＋TTS）照舊 180 秒。
        # `answer`（主人接她打來的）同理：只是掛上通話，不跑回合——以前沒列進來，brain 卡住時主人會停在
        # 「接続中…」最久 180 秒（審核 OC-2）；`hangup` 也是收尾動作，一起短。
        timeout = min(self.timeout, RING_TIMEOUT_S) \
            if action in {"ring", "start", "answer", "hangup"} else self.timeout
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                error = str(json.loads(exc.read()).get("error") or exc.code)
            except Exception:
                error = str(exc.code)
            if exc.code in {400, 403, 404, 409, 410, 422, 503}:
                raise BoardError(exc.code, error) from None
            raise BoardError(502, "brain_voice_http_" + error) from None
        except (OSError, ValueError) as exc:
            raise BoardError(503, "brain_voice_unavailable") from exc


@dataclass
class CallSession:
    id: str
    account_id: str
    principal: str
    entity: str
    language: str = ""
    state: str = "live"
    epoch: int = 0
    next_frame: int = 1
    next_utterance: int = 1
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    ended_at: float = 0.0
    #: 響鈴結果（`ringing` 期間為空）：`free`／`woken`… 或 `declined`；`line` 是她拒接時說的那句。
    pickup: str = ""
    line: str = ""
    reason: str = ""
    #: 誰打的：`owner`（主人打給她）／`entity`（她打給主人，見 `voice_outgoing.py`）
    caller: str = "owner"
    topic: str = ""
    expires_at: float = 0.0
    cutter: EnergyCutter = field(default_factory=EnergyCutter)
    events: deque = field(default_factory=deque)
    event_seq: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)
    condition: threading.Condition = field(init=False)
    turns: queue.Queue = field(default_factory=queue.Queue)

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)

    def public(self) -> dict:
        return {"id": self.id, "entity": self.entity, "end": "phone", "state": self.state,
                "epoch": self.epoch, "created_at": round(self.created_at, 3),
                "caller": self.caller}


class AppVoiceService(OutgoingCalls):
    def __init__(self, accounts, bridge, *, log=None) -> None:
        self.accounts = accounts
        self.bridge = bridge
        self.log = log or (lambda _message: None)
        self._sessions = {}
        #: 正在響鈴（還沒決定接不接）的帳號
        self._dialing = set()
        self._lock = threading.RLock()
        self._stopping = False

    def _get(self, call_id: str, account, *, touch: bool = False) -> CallSession:
        session = self._sessions.get(str(call_id))
        if session is None or session.account_id != account.id:
            raise BoardError(404, "call_not_found")
        with session.lock:
            if session.state != "live":
                raise BoardError(410, "call_ended")
            if time.time() - session.last_seen > IDLE_S:
                self._end_locked(session, "idle")
                raise BoardError(410, "call_ended")
            if touch:
                session.last_seen = time.time()
            return session

    def _purge(self) -> None:
        now = time.time()
        expired = []
        with self._lock:
            stale = []
            for key, session in self._sessions.items():
                if session.state == "live" and now - session.last_seen > IDLE_S:
                    with session.lock:
                        self._end_locked(session, "idle")
                    expired.append(session)
                elif session.state not in {"live", "ringing", "connecting"} \
                        and now - (session.ended_at or session.created_at) > ENDED_TTL_S:
                    stale.append(key)
            for key in stale:
                self._sessions.pop(key, None)
        # 异常退出的客户端不会发 hangup；下次重拨时同时收掉 brain 的旧通话。
        for session in expired:
            try:
                self.bridge.call("hangup", self._payload(session))
            except BoardError as exc:
                self.log(f"app voice idle hangup brain: {exc.code}")

    def open(self, account, entity: str, language: str = "") -> dict:
        self._purge()
        if account.kind != "owner":
            raise BoardError(403, "forbidden")
        target = self.accounts.get(str(entity))
        if target is None or target.kind != "entity":
            raise BoardError(404, "entity_not_found")
        if account.visible_entities is not None and target.id not in account.visible_entities:
            raise BoardError(403, "call_entity_forbidden")
        with self._lock:
            if self._busy_locked(account.id):            # 她正打過來（響鈴中）也算佔線
                raise BoardError(409, "busy_elsewhere")
            self._dialing.add(account.id)       # 響鈴期間佔住這個帳號，不另撥第二通
        try:
            call_id = "call_" + secrets.token_hex(12)
            session = CallSession(call_id, account.id, account.legacy_principal or account.id,
                                  target.id, str(language or "")[:16])
            # **兩階段響鈴**（owner 2026-11-17 晚）：立刻回 `ringing`，她接不接在背景決定。
            # 為什麼改：以前 POST 同步等到她決定才回應，她 0 ms 就接 ⇒ 客戶端根本沒有響鈴期間，
            # 看起來像「沒有走響鈴邏輯」（實測第二次撥號）。現在響鈴時間由客戶端掌控，
            # 決定透過事件流／`/state` 下發。
            session.state = "ringing"
            with self._lock:
                self._sessions[call_id] = session
            threading.Thread(target=self._settle, args=(session, account.id), daemon=True,
                             name="app-voice-ring-" + call_id[-6:]).start()
            return {"session": session.public(), "state": "ringing", "audio": self.audio_format()}
        except Exception:
            with self._lock:
                self._dialing.discard(account.id)
            raise

    @staticmethod
    def audio_format() -> dict:
        return {"encoding": "pcm_s16le", "sample_rate": RATE, "channels": 1,
                "frame_ms": 100, "first_seq": 1}

    def tts_status(self) -> dict:
        """語音引擎（TTS 池）健康——通話沒聲音時，客戶端要能說出「不是她不理你」。

        走 brain 問（池的設定只在 agent 那一側）；快取 30 秒，避免每次撥號都多一次往返。
        """
        now = time.time()
        cached = getattr(self, "_tts_cache", None)
        if cached and now - cached[0] < 30.0:
            return cached[1]
        try:
            report = self.bridge.call("voice_status", {})
            if not isinstance(report, dict):
                report = {"ok": None}
        except BoardError as exc:
            report = {"ok": None, "error": exc.code}
        result = {**report, "checked_at": round(now, 3)}
        self._tts_cache = (now, result)
        return result

    def _settle(self, session: CallSession, account_id: str) -> None:
        """響鈴期間的決定（背景）：接 ⇒ 轉 `live` 並起回合執行緒；不接 ⇒ `declined` 留 `line`。"""
        declined = None
        try:
            pickup = self._ring(session)
        except BoardError as exc:
            if exc.code != "call_declined":
                with session.lock:
                    session.state, session.pickup = "failed", exc.code
                    self._emit_locked(session, "call", {**session.public(), "pickup": "failed",
                                                        "reason": exc.code})
                self.log(f"app voice 響鈴失敗：{session.id} {exc.code}")
                return
            declined = (str(exc.extra.get("reason") or "declined"),
                        str(exc.extra.get("line") or ""))
            pickup = "declined"
        finally:
            with self._lock:
                self._dialing.discard(account_id)
        with session.lock:
            if declined is not None:
                session.state, session.pickup = "declined", "declined"
                session.reason, session.line = declined[0], declined[1]
                self._emit_locked(session, "call", {**session.public(), "pickup": "declined",
                                                    "reason": declined[0], "line": declined[1]})
                self.log(f"app voice 沒接：{session.id} reason={declined[0]}")
                return
            if session.state != "ringing":
                return
            session.state, session.pickup = "live", pickup
            session.last_seen = time.time()
            self._emit_locked(session, "call", {**session.public(), "pickup": pickup,
                                                "answered": True})
        threading.Thread(target=self._run, args=(session,), daemon=True,
                         name="app-voice-" + session.id[-6:]).start()

    def state(self, account, call_id: str) -> dict:
        """響鈴／通話的當前狀態（客戶端在響鈴期間輪詢這一支）。"""
        session = self._sessions.get(str(call_id))
        if session is None or not self._visible(session, account):   # 她打出去的那一通她也能問
            raise BoardError(404, "call_not_found")
        with session.lock:
            return {"session": session.public(), "state": session.state,
                    "pickup": session.pickup, "line": session.line,
                    "reason": session.reason, "expires_at": round(session.expires_at, 3)}

    def _ring(self, session: CallSession) -> str:
        """問 brain 接不接。接 ⇒ 回理由字（`free`／`woken`…）；不接 ⇒ 409 `call_declined`。"""
        payload = self._payload(session)
        try:
            reply = self.bridge.call("ring", payload)
        except BoardError as exc:
            if exc.status != 400 or exc.code != "bad_voice_action":
                raise
            # brain 還是舊版（不認得 ring）：照舊直接接
            self.bridge.call("start", payload)
            return "legacy"
        if not isinstance(reply, dict) or reply.get("answer", True):
            return str((reply or {}).get("reason") or "answered")
        reason = str(reply.get("reason") or "declined")
        reason = reason if reason in DECLINE_REASONS else "declined"
        line = str(reply.get("line") or "")[:120]
        self.log(f"app voice 沒接：{session.id} entity={session.entity} reason={reason}")
        raise BoardError(409, "call_declined", reason=reason, line=line)

    @staticmethod
    def _payload(session: CallSession) -> dict:
        return {"call_id": session.id, "account_id": session.account_id,
                "principal": session.principal, "entity": session.entity,
                "language": session.language, "epoch": session.epoch}

    def ping(self, account, call_id: str) -> dict:
        session = self._get(call_id, account, touch=True)
        try:
            self.bridge.call("ping", self._payload(session))
        except BoardError as exc:
            if exc.status == 410:
                with session.lock:
                    self._end_locked(session, "brain_ended")
            raise
        with session.lock:
            return {"session": session.public()}

    def feed(self, account, call_id: str, seq, pcm_b64: str) -> dict:
        session = self._get(call_id, account, touch=True)
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            raise BoardError(400, "bad_frame_seq") from None
        try:
            pcm = base64.b64decode(str(pcm_b64 or ""), validate=True)
        except (ValueError, TypeError):
            raise BoardError(400, "bad_audio") from None
        if not pcm or len(pcm) > MAX_FRAME_BYTES or len(pcm) % 2:
            raise BoardError(400, "bad_audio_frame", limit=MAX_FRAME_BYTES)
        jobs = []
        brain_payload = None
        with session.lock:
            if seq < session.next_frame:
                return {"ok": True, "duplicate": True, "next_seq": session.next_frame,
                        "epoch": session.epoch}
            if seq != session.next_frame:
                raise BoardError(409, "frame_out_of_order", expected=session.next_frame)
            session.next_frame += 1
            for kind, clip in session.cutter.feed(pcm):
                if kind == "started":
                    session.epoch += 1
                    self._emit_locked(session, "interrupt", {
                        "call_id": session.id, "epoch": session.epoch, "reason": "barge_in"})
                    # ⚠️ 叫醒 brain 的那一步是網路 I/O，**不能**在 session.lock 裡做（見迴圈外）
                    brain_payload = self._payload(session)
                elif kind == "utterance" and clip:
                    if len(clip) > MAX_UTTERANCE_BYTES:
                        clip = clip[-MAX_UTTERANCE_BYTES:]
                    utterance_id = f"{session.id}:{session.next_utterance}"
                    session.next_utterance += 1
                    jobs.append((utterance_id, session.epoch, pcm16_wav(clip)))
                    self._emit_locked(session, "utterance", {
                        "id": utterance_id, "epoch": session.epoch, "state": "queued"})
                elif kind == "dropped":
                    self._emit_locked(session, "activity", {"state": "listening"})
            for job in jobs:
                session.turns.put(job)
            reply = {"ok": True, "next_seq": session.next_frame, "epoch": session.epoch,
                     "utterances": [job[0] for job in jobs]}
        if brain_payload is not None:
            # 打斷要通知 brain，但這是網路 I/O：**在鎖外、非同步**做。
            # 2026-11-08 實測（owner 手機）：鎖內同步呼叫會把其他幀請求一起卡住，
            # 手機每 100 ms 一幀、串行等回包 ⇒ 一秒只能送 2 幀 ⇒ 積壓超過客戶端 50 幀上限，
            # 通話 7 秒就自己「通話が切れました」（`NextCallView` 的 `_frames.Count > 50`）。
            threading.Thread(target=self._interrupt_brain, args=(brain_payload,),
                             daemon=True, name="app-call-interrupt").start()
        return reply

    def _interrupt_brain(self, payload: dict) -> None:
        """叫 brain 停嘴：只保證「不再有新聲音」，失敗就算了（epoch 閘本來就擋住舊音）。"""
        try:
            self.bridge.call("interrupt", payload)
        except BoardError:
            pass

    def interrupt(self, account, call_id: str) -> dict:
        session = self._get(call_id, account, touch=True)
        with session.lock:
            session.cutter.reset()               # 手动静音／打断也丢弃未闭合的半句
            session.epoch += 1
            self._emit_locked(session, "interrupt", {
                "call_id": session.id, "epoch": session.epoch, "reason": "manual"})
            payload = self._payload(session)
            epoch = session.epoch
        reply = self.bridge.call("interrupt", payload)   # 同樣不放在鎖裡
        return {"ok": True, "epoch": epoch, "brain": reply}

    def hangup(self, account, call_id: str) -> dict:
        session = self._sessions.get(str(call_id))
        if session is None or not self._visible(session, account):
            raise BoardError(404, "call_not_found")
        if session.caller == "entity" and session.state == "ringing":
            # 她打來的響鈴中：她掛 ⇒ 取消；主人掛 ⇒ 等於拒接
            if session.account_id != account.id:
                return self.cancel_out(session)
            return self.decline(account, call_id)
        if session.account_id != account.id:
            account = self.accounts.get(session.account_id) or account   # 她收掉已接通的那一通
        with session.lock:
            if session.state == "ringing":
                # 響鈴中途掛斷：她還在決定，這裡直接作廢；`_settle` 看到非 ringing 就不會接。
                session.state = "cancelled"
                self._emit_locked(session, "call", {**session.public(), "reason": "hangup"})
                return {"ok": True, "session": session.public()}
        session = self._get(call_id, account)
        with session.lock:
            self._end_locked(session, "hangup")
            session.turns.put(None)
        try:
            self.bridge.call("hangup", self._payload(session))
        except BoardError as exc:
            self.log(f"app voice hangup brain: {exc.code}")
        return {"ok": True, "session": session.public()}

    def _end_locked(self, session: CallSession, reason: str) -> None:
        if session.state != "live":
            return
        session.state = "ended"
        session.ended_at = time.time()
        session.cutter.reset()
        # 取證用：誰把這通收掉的（idle／hangup／brain_ended）。2026-11-08 通話 7 秒自斷，
        # 沒有這行就只能猜，加上之後一次通話就能定性。
        self.log(f"app voice 收線：{session.id} reason={reason} "
                 f"frames={session.next_frame - 1} utterances={session.next_utterance}")
        self._emit_locked(session, "call", {**session.public(), "reason": reason})
        session.turns.put(None)

    def _emit_locked(self, session: CallSession, name: str, payload: dict) -> None:
        session.event_seq += 1
        session.events.append({"seq": session.event_seq, "name": name, "payload": payload})
        while len(session.events) > EVENT_LIMIT:
            session.events.popleft()
        session.condition.notify_all()

    def events(self, account, call_id: str, after: int = 0, *, wait: float = 0.0) -> dict:
        session = self._sessions.get(str(call_id))
        if session is None or session.account_id != account.id:
            raise BoardError(404, "call_not_found")
        with session.lock:
            if session.state == "live" and time.time() - session.last_seen > IDLE_S:
                self._end_locked(session, "idle")
                session.turns.put(None)
            first = session.events[0]["seq"] if session.events else session.event_seq + 1
            if after < first - 1:
                raise BoardError(409, "call_cursor_expired", next_cursor=session.event_seq)
            deadline = time.monotonic() + max(0.0, wait)
            selected = [event for event in session.events if event["seq"] > after]
            while not selected and session.state == "live" and time.monotonic() < deadline:
                session.condition.wait(min(0.5, deadline - time.monotonic()))
                selected = [event for event in session.events if event["seq"] > after]
            return {"events": selected, "next_cursor": session.event_seq,
                    "state": session.state}

    def _run(self, session: CallSession) -> None:
        while True:
            job = session.turns.get()
            if job is None or self._stopping:
                return
            utterance_id, epoch, wav = job
            try:
                reply = self.bridge.call("utterance", {
                    **self._payload(session), "utterance_id": utterance_id,
                    "audio": base64.b64encode(wav).decode("ascii"), "format": "wav"})
                events = reply.get("events") or []
            except BoardError as exc:
                events = [{"name": "error", "payload": {"reason": exc.code}}]
            with session.lock:
                if session.state != "live" or session.epoch != epoch:
                    continue                         # 挂断或 barge-in 后的旧音频绝不播放
                for event in events:
                    name = str(event.get("name") or "frame")
                    payload = dict(event.get("payload") or {})
                    payload.update({"call_id": session.id, "utterance_id": utterance_id,
                                    "epoch": epoch})
                    self._emit_locked(session, name, payload)

    def close(self) -> None:
        self._stopping = True
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            with session.lock:
                session.state = "ended"
                session.condition.notify_all()
                session.turns.put(None)


__all__ = ["AppVoiceService", "BrainVoiceBridge", "EnergyCutter", "pcm16_wav",
           "RATE", "FRAME_BYTES", "MAX_FRAME_BYTES"]
