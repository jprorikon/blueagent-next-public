"""她沒接主人的電話之後（owner 2026-11-17）：留痕，必要時改發一則私聊。

`runtime/inner/call_pickup.py` 決定不接時，`app_voice._ring` 立刻把理由回給 App Server（App 顯示畫面），
這裡在背景做收尾，**不拖慢響鈴的回包**：

  · `declined`（她看了狀況決定不接）⇒ 那一句當**主動私聊**送進主人↔她的 direct 會話
    （`ProactiveGate.maybe_send(force=True)` → `delivery` → `app_proactive.AppConversationChannel`），
    主人回到聊天頁就看得到、有未讀；送出後照主動消息的老路寫回她自己的線，之後她記得自己說過。
  · `asleep`／`on_call` ⇒ 不發訊息（睡著的人不會回訊息；話し中本來就看得出來），只在她的私人行動日誌
    記一筆「有電話、沒接到」，醒來／講完之後的她能從日誌知道。

**行動日誌記的是結果，不是意圖**（審核 R3，2026-11-17）：以前先寫「…と送った」才去送，
沒有發送端、送的時候拋例外、`sent=False` 都還是留下「已發送」——之後自我模型／活動摘要會把沒送到的話
當成已經跟主人說過。現在一筆日誌、在送完之後寫：送到 ⇒ 「…と送った」；沒送到 ⇒ 「送ろうとしたが届かなかった」＋原因。
"""

from __future__ import annotations

import threading

NOTES = {
    "asleep": "寝ている間にオーナーから電話があった（出られなかった）",
    "on_call": "別の通話中にオーナーから電話があった（出られなかった）",
    "declined": "オーナーから電話があったけど、出なかった",
}
SENT = "：「{line}」と送った"
NOT_SENT = "（「{line}」と送ろうとしたが、届かなかった）"


def after(state, entity: str, principal: str, decision, *, wait: bool = False) -> None:
    """背景收尾；`wait=True` 給測試同步跑完。"""
    worker = threading.Thread(target=_finish, args=(state.app, str(entity), str(principal), decision),
                              daemon=True, name="call-decline")
    worker.start()
    if wait:
        worker.join(5.0)


def _finish(app, entity: str, principal: str, decision) -> None:
    reason = str(getattr(decision, "reason", "") or "")
    line = str(getattr(decision, "line", "") or "").strip()
    note = NOTES.get(reason)
    if not note:
        return
    if reason != "declined" or not line:
        _log(app, entity, note, {"reason": reason, "principal": principal})
        return
    delivered, failure = _send(app, entity, principal, line)
    text = note + (SENT if delivered else NOT_SENT).format(line=line)
    extra = {"reason": reason, "principal": principal, "delivered": delivered}
    if not delivered:
        extra["failure"] = failure
    _log(app, entity, text, extra)


def _send(app, entity: str, principal: str, line: str) -> tuple[bool, str]:
    """把拒接那一句當主動私聊送出去 ⇒ `(送到了沒, 沒送到的原因)`。"""
    gate = getattr(app, "proactive", None)
    delivery = getattr(app, "delivery", None)
    if gate is None or delivery is None:
        print("  call decline: 私聊沒送出（沒有發送端）", flush=True)
        return False, "no_channel"
    try:
        outcome = gate.maybe_send(entity, line, principal=principal, publish=delivery.send,
                                  force=True, dedupe=False)
    except Exception as exc:                              # noqa: BLE001
        print(f"  call decline: 私聊沒送出（{type(exc).__name__}）", flush=True)
        return False, f"error:{type(exc).__name__}"
    if not (outcome or {}).get("sent"):
        why = str((outcome or {}).get("reason") or "not_sent")
        print(f"  call decline: 私聊沒送出（{why}）", flush=True)
        return False, why
    return True, ""


def _log(app, entity: str, text: str, extra: dict) -> None:
    log = getattr(app, "activity_log", None)
    if log is None:
        return
    try:
        log.append(entity, kind="call_missed", text=text, extra=extra)
    except Exception as exc:                              # noqa: BLE001 - 記不上不影響通話
        print(f"  call decline: 行動日誌沒記上（{type(exc).__name__}）", flush=True)
