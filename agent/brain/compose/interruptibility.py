"""主人此刻能不能被打擾（common-ground 第 8 步，docs/PLAN-common-ground-20261109.md §5.2）。

AEM 政策裡早就有 `interruptibility_ttl_s`／`respect_do_not_interrupt`，但全倉沒有任何代碼產生這個信號；
主動消息的閘只看安靜時段、她的生物鐘、間隔。這裡補上兩個**看得到的**信號（全是規則，不調模型）：

  · `owner_in_call`：主人正在跟任何一個角色通話（App 通話／Discord 語音）——這時候插一則文字是打擾；
  · `owner_busy_elsewhere`：主人這 10 分鐘在**別的地方**說過話（跟另一個角色私聊、在群裡）——
    他正在跟別人聊，她突然開新話題就是「突兀打擾」。

（2026-11-09 曾有第三個「主人說了おやすみ就靜默 9 小時」；owner 同日拿掉：她們有自己的生物鐘，
她發了、主人在睡，那是主人自己的靜默。）

只回「為什麼現在不行」（空字串＝可以），由 `ProactiveGate.allows()` 決定擋不擋。
"""

from __future__ import annotations

import os
import time


def _seconds(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name) or default))
    except ValueError:
        return float(default)


def make_owner_busy(*, threads, voice=None, entities=(), clock=time.time,
                    elsewhere_s: float | None = None):
    """回 `busy(entity, principal, now) -> reason`（空字串＝可以打擾）。"""
    elsewhere = _seconds("BLUEAGENT_OWNER_ELSEWHERE_S", 600) if elsewhere_s is None else elsewhere_s
    names = [str(item) for item in (entities or ())]

    def in_call(principal: str) -> bool:
        if voice is None:
            return False
        for name in names:
            try:
                session = voice.active(name)
            except Exception:                              # noqa: BLE001
                session = None
            if session is not None and str(getattr(session, "principal", "") or "") == principal:
                return True
        return False

    def busy_elsewhere(entity: str, principal: str, now: float) -> bool:
        if not elsewhere:
            return False
        try:
            rows = list(threads.threads())
        except Exception:                                  # noqa: BLE001
            return False
        for thread in rows:
            key = getattr(thread, "key", None)
            mine = str(getattr(key, "entity", "")) == str(entity)
            group = str(getattr(key, "scope", "") or "").startswith("group:")
            if mine and not group:
                continue                                   # 跟她自己的私聊不算「別的地方」
            for turn in reversed(list(getattr(thread, "transcript", ()) or ())[-6:]):
                if str(getattr(turn, "speaker", "") or "") != principal:
                    continue
                if now - float(getattr(turn, "at", 0.0) or 0.0) <= elsewhere:
                    return True
                break
        return False

    def busy(entity: str, principal: str = "owner", now: float | None = None) -> str:
        at = float(now if now is not None else clock())
        who = str(principal or "owner")
        if in_call(who):
            return "owner_in_call"
        if busy_elsewhere(entity, who, at):
            return "owner_busy_elsewhere"
        return ""

    return busy


def wire(app, *, log=None) -> None:
    """把信號接到 `app.proactive.interrupt`（`BLUEAGENT_OWNER_INTERRUPTIBILITY=0` ⇒ 不接）。"""
    proactive = getattr(app, "proactive", None)
    if proactive is None:
        return
    if str(os.environ.get("BLUEAGENT_OWNER_INTERRUPTIBILITY") or "1").strip().lower() \
            in {"0", "off", "false", "no"}:
        proactive.interrupt = None
        return
    registry = getattr(app, "entities", None)
    names = [entity.id for entity in registry.all()] if registry is not None else []
    proactive.interrupt = make_owner_busy(threads=getattr(app, "threads", None),
                                          voice=getattr(app, "voice", None), entities=names)


__all__ = ["make_owner_busy", "wire"]
