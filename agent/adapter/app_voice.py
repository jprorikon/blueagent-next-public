"""App Server 实时语音 pipe：完整 utterance → STT／对话／TTS → 平台事件。

共享密钥在 HTTP 边界校验；这里只接受 App Server 已鉴权的账号与通话。平台负责切句、
帧顺序、ACL 和客户端投递，brain 不读取 App 数据库，也不把实时帧写成社交消息。
"""

from __future__ import annotations

import base64

from agent.adapter import app_call_decline
from agent.brain.domain.voice import VoiceSession, VoiceState

MAX_WAV_BYTES = 16 * 16000 * 2 + 4096


def _required(body: dict) -> tuple[str, str, str, str]:
    call_id = str(body.get("call_id") or "").strip()
    account_id = str(body.get("account_id") or "").strip()
    principal = str(body.get("principal") or "").strip()
    entity = str(body.get("entity") or "").strip()
    if not all((call_id, account_id, principal, entity)) or max(
            map(len, (call_id, account_id, principal, entity))) > 128:
        raise ValueError("bad_voice_session")
    return call_id, account_id, principal, entity


def _active(state, body: dict):
    call_id, _account_id, principal, entity = _required(body)
    session = state.app.voice.active(entity)
    if session is None or session.id != call_id or session.principal != principal \
            or session.target != "app:" + call_id:
        return None
    state.app.voice.touch(entity, call_id)
    return session


def _session(call_id: str, account_id: str, principal: str, entity: str,
             note: str = "", pickup: str = "") -> VoiceSession:
    meta = {"app_account_id": account_id}
    if note:
        meta["pickup_note"] = note            # 第一句的 `[[room]]`（被電話吵醒…），用一次就丟
    if pickup:
        meta["pickup"] = pickup
    return VoiceSession(
        id=call_id, entity=entity, end="phone", target="app:" + call_id,
        state=VoiceState.CONNECTED, reason="app_platform", principal=principal,
        thread_id=call_id, meta=meta)


def _picker(state):
    """組合根接好的 `CallPickup`；測試基座／舊組合沒有 ⇒ 建一個只用規則的（不叫模型）。"""
    found = getattr(state.app, "call_pickup", None)
    if found is None:
        from agent.brain.runtime.inner.call_pickup import CallPickup
        found = CallPickup()
        try:
            state.app.call_pickup = found
        except AttributeError:
            pass
    return found


def _ring(state, call_id: str, account_id: str, principal: str, entity: str) -> tuple[int, dict]:
    """響鈴：她決定接不接（`runtime/inner/call_pickup.py`）。接 ⇒ 掛上通話；不接 ⇒ 回理由與一句。"""
    existing = state.app.voice.active(entity)
    if existing is not None and existing.id == call_id:
        return 200, {"ok": True, "answer": True, "reason": "existing", "session": existing.to_dict()}
    decision = _picker(state).decide(state.app, entity, principal, call_id)
    if decision.answer:
        reply = state.app.voice.attach(
            _session(call_id, account_id, principal, entity, decision.note, decision.reason))
        if reply.get("ok"):
            return 200, {**reply, "answer": True, "reason": decision.reason}
        # 判斷與掛上之間被別的通話搶先：一樣當「話し中」
        decision = type(decision)(False, "on_call", signals=decision.signals)
    app_call_decline.after(state, entity, principal, decision)
    return 200, {"ok": True, "answer": False, "reason": decision.reason, "line": decision.line}


def _answer(state, call_id: str, account_id: str, principal: str, entity: str,
            body: dict) -> tuple[int, dict]:
    """她打給主人、主人接了（App Server 的 `accept`）：掛上她這頭的通話，第一句知道是自己打的。"""
    from agent.adapter.app_call_out import ANSWERED_NOTE

    dialer = getattr(state.app.voice, "dialer", None)
    pending = dialer.pending(call_id) if dialer is not None and hasattr(dialer, "pending") else None
    why = str(getattr(pending, "reason", "") or body.get("topic") or "").strip()[:120]
    note = ANSWERED_NOTE.format(why=f"かけた理由：{why}" if why else "")
    reply = state.app.voice.attach(_session(call_id, account_id, principal, entity, note, "outgoing"))
    return (200, {**reply, "answer": True}) if reply.get("ok") else (409, reply)


def handle(state, body: dict) -> tuple[int, dict]:
    action = str(body.get("action") or "")

    if action == "voice_status":
        # 語音引擎健康（owner 2026-11-17）：通話沒聲音時要能分辨「TTS 掛了」而不是她不理人。
        tts = getattr(getattr(state.app, "voice", None), "tts", None)
        if tts is None or not hasattr(tts, "health"):
            return 200, {"ok": None, "backend": "off"}
        try:
            report = dict(tts.health())
            if "ok" not in report:
                # 池的 health() 只回每個 instance 的 alive：這裡統一成一個結論（任何一台活著就算活）
                instances = report.get("instances") or []
                report["ok"] = any(bool(item.get("alive")) for item in instances)
            return 200, report
        except Exception as exc:                            # noqa: BLE001
            return 200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        call_id, account_id, principal, entity = _required(body)
    except ValueError:
        return 400, {"error": "bad_voice_session"}

    if action == "ring":
        return _ring(state, call_id, account_id, principal, entity)

    if action == "answer":
        return _answer(state, call_id, account_id, principal, entity, body)

    if action == "start":
        # 舊合同：不問、直接接（App Server 舊版／測試基座）。新版平台走 `ring`。
        reply = state.app.voice.attach(_session(call_id, account_id, principal, entity))
        return (200, reply) if reply.get("ok") else (409, reply)

    if action == "hangup":
        # 挂断幂等：brain 已因空闲先收线时，平台仍能完成自己的收尾。
        state.app.voice.hangup(entity, call_id=call_id, reason="app_hangup")
        return 200, {"ok": True}

    session = _active(state, body)
    if session is None:
        return 410, {"error": "call_ended"}

    if action == "ping":
        return 200, {"ok": True, "session": session.to_dict()}

    if action == "interrupt":
        reply = state.app.voice.interrupt(entity, reason="app_barge_in", principal=principal)
        return 200, {"ok": True, "epoch": reply["epoch"]}

    if action != "utterance":
        return 400, {"error": "bad_voice_action"}
    utterance_id = str(body.get("utterance_id") or "").strip()
    if not utterance_id or len(utterance_id) > 160:
        return 400, {"error": "bad_utterance_id"}
    try:
        wav = base64.b64decode(str(body.get("audio") or ""), validate=True)
    except (ValueError, TypeError):
        return 400, {"error": "bad_audio"}
    if len(wav) < 44 or len(wav) > MAX_WAV_BYTES or wav[:4] != b"RIFF":
        return 400, {"error": "bad_audio"}
    stt = getattr(state.app, "stt", None)
    if stt is None:
        return 503, {"error": "stt_unavailable"}
    language = str(body.get("language") or "").strip().lower()
    transcript = stt.transcribe(wav, language=language, suffix=".wav")
    if not transcript.ok:
        return 422, {"error": "stt_failed", "transcript": transcript.to_dict()}
    # STT 完成后再次核对：处理期间挂断的通话不能变成普通聊天或 delayed delivery。
    if _active(state, body) is None:
        return 410, {"error": "call_ended"}
    events = [{"name": "transcript", "payload": transcript.to_dict()}]
    stream = state.send_stream(
        text=transcript.text, entity=entity, principal=principal, mode="chat",
        request_id=utterance_id, want_voice=True, source_override="phone:call",
        room_note=str(session.meta.pop("pickup_note", "") or ""))
    for kind, payload in stream:
        if kind == "frame" and isinstance(payload, dict):
            kind = str(payload.get("kind") or "frame")
        if isinstance(payload, dict):
            events.append({"name": str(kind), "payload": dict(payload)})
        else:
            events.append({"name": str(kind), "payload": {"value": payload}})
    return 200, {"ok": True, "events": events}


__all__ = ["handle", "MAX_WAV_BYTES"]
