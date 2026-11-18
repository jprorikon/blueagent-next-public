"""可獨立於應用狀態使用的時間文字規則。"""

from __future__ import annotations

import time


def human_age(seconds: float, *, now: float | None = None) -> str:
    """秒數 → 日語的相對時間；負數（未來）視為「さっき」。"""
    age = max(0.0, float(seconds or 0.0))
    if age < 90:
        return "さっき"
    if age < 3600:
        return f"{int(age // 60)} 分前"
    if age < 6 * 3600:
        return f"{int(age // 3600)} 時間前"
    moment = float(now) - age if now is not None else time.time() - age
    stamp = time.localtime(moment)
    today = time.localtime(now if now is not None else time.time())
    yesterday = time.localtime(time.mktime(today[:3] + (12, 0, 0, 0, 0, -1)) - 86400)
    if stamp[:3] == yesterday[:3]:
        return time.strftime("昨日 %H:%M", stamp)
    if stamp.tm_year == today.tm_year:
        return f"{stamp.tm_mon}月{stamp.tm_mday}日 " + time.strftime("%H:%M", stamp)
    return time.strftime("%Y-%m-%d %H:%M", stamp)


__all__ = ["human_age"]
