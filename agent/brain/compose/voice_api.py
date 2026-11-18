"""語音控制：App 的單人通話（撥／心跳／打斷／掛），與掛斷。她自己撥打／說話仍由 brain 的 tool_call 觸發。

App 通話（owner 2026-10-29「單人通話模式，實時和 agent 對話」，只給 master）：
  · `call`：拿一通 `end=phone` 的 VoiceSession ⇒ 之後她的回覆**一律出聲**（粘性）；
  · 主人每說完一句，App 照常打 `/api/audio`，多帶 `X-Call: <call_id>`；
  · `interrupt`：主人在她說話時開口 ⇒ 伺服器不再合成這一輪剩下的句子（端末自己停播）；
  · `ping`：App 每 15 秒一次；斷太久（`BLUEAGENT_CALL_IDLE_S`）就自動掛；
  · `hangup`：掛斷，放掉發言權。

兩個端末（手機 `phone`／MR 頭顯 `mr`，`domain/client_end.py`）各撥各的一通（`end` 由 `X-Source` 決定）；
一個角色同時只有一通 ⇒ **後撥的那一端蓋掉先撥的**（owner 2026-11-01）：先撥的那端下一次心跳（或送一句）拿到
`410 taken_over`、退出並明示「被其他終端接管」（WO-mr-call-and-switch §7 W3）；別的原因沒了的一通照舊 `410 call_ended`。
Discord 語音房不在此列（她真的坐在房裡）：照舊回 `busy_elsewhere`。
"""

from __future__ import annotations

from agent.brain.domain.client_end import APP_ENDS, PHONE
from agent.brain.runtime.delivery.outbox import new_turn_id


#: 被另一端接管（後撥的蓋掉先撥的）：410 的錯誤碼，跟「自己斷了」（`call_ended`）分開
TAKEN_OVER = "taken_over"


def call(state, entity: str, principal: str, end: str = PHONE) -> tuple[int, dict]:
    voice = state.app.voice
    current = voice.active(entity)
    if current is not None and current.end in APP_ENDS and current.end != end:
        # 後撥的蓋掉先撥的：先掛另一端那一通（手機那通照常留通話標記），再撥這一端的
        _end_call(state, entity, current.id, reason=TAKEN_OVER)
        print(f"[call] {entity}：{end} 撥入 ⇒ 掛掉 {current.end} 那一通（{current.id}）", flush=True)
    result = voice.call(entity, end=end, reason="app", principal=principal)
    session = result.get("session") or {}
    if not result.get("ok"):
        return 409, {"ok": False, "error": str(result.get("error") or "call_failed")}
    if session.get("end") != end:
        # 她正在別的地方講電話（Discord 語音房）：一張嘴不能同時接兩通
        return 409, {"ok": False, "error": "busy_elsewhere", "session": session}
    voice.touch(entity, session.get("id", ""))
    return 200, {"ok": True, "existing": bool(result.get("existing")), "session": session}


def ended_error(state, call_id: str) -> str:
    """這一通已經不在了：回 410 要帶的錯誤碼（被接管 ⇒ `taken_over`；其他——空閒超時、她掛、主人掛 ⇒ `call_ended`）。"""
    return TAKEN_OVER if state.app.voice.ended_reason(call_id) == TAKEN_OVER else "call_ended"


def ping(state, entity: str, call_id: str) -> tuple[int, dict]:
    session = state.app.voice.touch(entity, call_id)
    if session is None:
        return 410, {"ok": False, "error": ended_error(state, call_id)}
    return 200, {"ok": True, "session": session.to_dict()}


def interrupt(state, entity: str, principal: str) -> tuple[int, dict]:
    voice = state.app.voice
    voice.touch(entity)
    return 200, {"ok": True, **voice.interrupt(entity, reason="app_barge_in",
                                               principal=principal)}


def _end_call(state, entity: str, call_id: str, reason: str = "") -> dict:
    """真的掛掉一通（放發言權、記原因）＋留通話標記；不組回應（撥號裡的接管只要這一步）。"""
    result = state.app.voice.hangup(entity, call_id=call_id, reason=reason)
    if result.get("ok"):
        _write_call_log(state, entity, result.get("session") or {})
    return result


def hangup(state, entity: str = "mira", call_id: str = "", reason: str = "") -> dict:
    """`POST /api/voice/hangup` 的回應：只回語音這一塊（輕）。

    ⚠️ 以前還附整份 `state.snapshot()`（記憶、對話線、episode…）＋她最後一句的 base64 音檔兩份 ⇒
    實機回應數百 KB～數 MB，掛斷的 200 要 11–31 秒才到（2026-11-01 MR／手機實測），撥號裡的接管也慢 2 秒多。
    要整份狀態的（網頁除錯台）掛完自己打 `GET /api/state`。
    """
    result = _end_call(state, entity, call_id, reason)
    payload = {"ok": bool(result.get("ok")), "voice": state.app.voice.snapshot(),
               "frames": result.get("frames") or []}
    if result.get("error"):
        payload["error"] = result["error"]
    return payload


#: 通話短於這個秒數就不留痕（誤觸、測試撥打）。
CALL_LOG_MIN_S = 3.0


def _duration_text(seconds: float) -> str:
    """`3分12秒` / `42秒`（給標記行用）。"""
    total = max(0, int(round(float(seconds or 0))))
    if total < 60:
        return f"{total}秒"
    return f"{total // 60}分{total % 60}秒"


def _write_call_log(state, entity: str, session: dict) -> None:
    """通話結束 ⇒ 在對話紀錄裡留一條**只有標記**的列（owner 2026-10-29）。

    通話的**內容**一率不進 App 的對話紀錄（`kind="call"` 由 `/api/turns` 濾掉）；
    這裡寫的是 `kind="call-log"`，文字只有時長 ⇒ 「發生過通話」。
    """
    outbox = getattr(state.app, "outbox", None)
    if outbox is None:
        return
    duration = float(session.get("duration_s") or 0.0)
    if duration < CALL_LOG_MIN_S:
        return
    # 只認 App 的通話（Discord 語音房本來就不在 App 的紀錄裡）
    if str(session.get("end") or "") != "phone":
        return
    try:
        outbox.record(
            turn_id=new_turn_id(),
            principal=str(session.get("principal") or "owner"),
            entity=str(entity),
            thread=str(session.get("thread_id") or ""),
            text=f"（通話 {_duration_text(duration)}）",
            source="phone:call",
            kind="call-log",
            # 客戶端拿秒數自己按界面語言說；`text` 只是給舊客戶端的退路。
            call_s=int(round(duration)),
        )
    except Exception as exc:                        # noqa: BLE001 - 留痕失敗不能擋掛斷
        print(f"[call] 通話標記寫入失敗（{type(exc).__name__}: {exc}）", flush=True)
