"""「開一件她自己的事」的兩個入口——從 `compose/bootstrap.py` 搬出來（守 600 行上限）。

  · `start_rotation()`：內心獨白選了 `action=work` ⇒ 按輪換開一件活動（owner 2026-11-01）。
  · `start_drive()`：內驅選中一條未結項（交付／停住的任務／沒驗的假設）⇒ 開一件「核這一條」的事
    （owner 2026-11-09）。標題與步驟都帶著那一條的原文，不是「你有線索」這種空話。

兩者都只在她手上**沒有進行中的事**時才開（`running`／`paused` 就讓它繼續，不插隊）。
"""

from __future__ import annotations


def _loop_for(entity: str, lives):
    return lives.get(str(entity)) if isinstance(lives, dict) else None


def _busy(loop) -> bool:
    try:
        status = str(((loop.snapshot() or {}).get("activity") or {}).get("status") or "")
    except Exception:                                    # noqa: BLE001
        return False
    return status in {"running", "paused"}


def start_rotation(entity: str, row: dict, *, lives=None) -> bool:
    """獨白想做事 ⇒ 開一件輪換活動。"""
    loop = _loop_for(entity, lives)
    if loop is None or _busy(loop):
        return False
    from agent.brain.runtime.life import build_activity
    activity = build_activity(str(entity), rotation=getattr(loop, "rotation", None))
    if activity is None:
        return False
    try:
        loop.start(activity)
    except Exception as exc:                             # noqa: BLE001
        print(f"  life[{entity}]: 開不成（{type(exc).__name__}: {exc}）", flush=True)
        return False
    print(f"  life[{entity}]: 獨白想做事 ⇒ 開「{activity.title}」", flush=True)
    return True


#: 沒有生物鐘的組合（測試／最小啟動）才用的安靜時段
FALLBACK_HOURS = (7, 23)


def drive_rest(affect, entity: str, *, now: float | None = None) -> bool:
    """心情要她先休息嗎（`runtime/inner/willing.py`，跟生活輪換同一條判據；這裡不記日誌，
    內驅被擋了自己會 15 分內不再問）。`BLUEAGENT_LIFE_MOOD_REST=0` 關。"""
    import os
    import time

    if affect is None or str(os.environ.get("BLUEAGENT_LIFE_MOOD_REST") or "1").strip().lower() \
            in {"0", "off", "false", "no"}:
        return False
    from agent.brain.runtime.inner import willing
    try:
        return bool(willing.rest_reason(affect.mood(str(entity)), entity=str(entity),
                                        now=time.time() if now is None else float(now)))
    except Exception:                                    # noqa: BLE001 - 心情壞了就照常
        return False


def _may_open(loop, entity: str, *, affect=None, now: float | None = None) -> bool:
    import time

    moment = time.time() if now is None else float(now)
    if hasattr(loop, "can_open_new") and getattr(loop, "_circadian", None) is not None:
        if not loop.can_open_new(moment):
            return False
    elif not (FALLBACK_HOURS[0] <= time.localtime(moment).tm_hour < FALLBACK_HOURS[1]):
        return False
    return not drive_rest(affect, entity, now=moment)


def start_drive(entity: str, signal: dict, *, lives=None, affect=None, now: float | None = None):
    """內驅選中一條 ⇒ 開一件「核這一條」的事（標題＋步驟都帶原文）。

    回：開了的活動 id（字串）／True（任務類：叫醒生活迴圈照台帳續做）／None（開不成）。
    生物鐘（睡著、睡前一小時、醒後半小時）與「心情不好先休息」照生活輪換同一套擋。
    """
    loop = _loop_for(entity, lives)
    if loop is None or _busy(loop) or not _may_open(loop, entity, affect=affect, now=now):
        return None
    if signal.get("kind") == "task":
        # 停住的任務：台帳本來就會「續 hanging → 撿 pending」，只是她手上空著在休息 ⇒ 叫醒就好，
        # 不另開一件泛泛的「進める」（那會跟台帳那件重複）
        if hasattr(loop, "wake_now") and loop.wake_now():
            print(f"  life[{entity}]: 內驅 ⇒ 叫醒，照台帳續「{str(signal.get('why') or '')[:30]}」",
                  flush=True)
            return True
        return None
    steps = [str(step) for step in (signal.get("steps") or []) if str(step).strip()]
    if not steps:
        return None
    from agent.brain.domain.activity import Activity
    activity = Activity.create(str(entity), str(signal.get("title") or "確かめる"), steps,
                               kind="drive")
    try:
        loop.start(activity)
    except Exception as exc:                             # noqa: BLE001 - 開不成不能擋心跳
        print(f"  life[{entity}]: 內驅開不成（{type(exc).__name__}: {exc}）", flush=True)
        return None
    print(f"  life[{entity}]: 內驅 ⇒ 開「{activity.title}」（{signal.get('kind')}）", flush=True)
    return str(activity.id)


def drive_running(entity: str, activity_id: str, *, lives=None) -> bool:
    """內驅開的那一件還在做嗎（結帳要等它做完）。沒 id（任務類叫醒）⇒ 看手上有沒有事。"""
    loop = _loop_for(entity, lives)
    if loop is None:
        return False
    try:
        current = ((loop.snapshot() or {}).get("activity") or {})
    except Exception:                                    # noqa: BLE001
        return False
    if not activity_id:
        return _busy(loop)
    return str(current.get("id") or "") == str(activity_id) and _busy(loop)


__all__ = ["drive_rest", "drive_running", "start_drive", "start_rotation"]
