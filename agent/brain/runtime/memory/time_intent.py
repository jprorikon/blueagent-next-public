"""問句裡的時間意圖：`memory.search` 要不要按某個時間點查（as-of），還是只是在問「以前」。

從 `memory_search.py` 拆出來（2026-11-16，PENDING B.Agent.4 第三步）：那支已經頂著 600 行棘輪，
而「看懂問句在講哪一天」本來就是獨立的一件事——它不碰庫、不碰向量，全是純函式，測試可以直接打。

三種寫法：
  ① 相對說法：昨天／先週／上個月／剛才 … ⇒ 現在往回推固定天數；
  ② 「N 天前」：3 天前／5日前／2 days ago；
  ③ **具體日期**（這一步新加的）：2026-11-03／2026年11月3日／11月3日／11月3号／11/3／
     上個月15号／先月15日／這個月3號／今月3日。沒寫年份的日期若落在未來，就當成去年。

回的時間點取那一天的**當地中午**：as-of 判的是「那一刻成立的版本」，取正午可以避開
一天頭尾的邊界（一天裡改過兩次的狀態，取中午那一版）。
"""

from __future__ import annotations

import re
import time

#: 有明確時間點的詞 ⇒ as-of 查詢（回那個時間點「當時成立」的版本）。值＝往回推幾天。
AS_OF_HINTS = {"昨天": 1, "昨日": 1, "前天": 2, "一昨日": 2, "上周": 7, "上週": 7, "先週": 7,
               "上個月": 30, "上个月": 30, "先月": 30, "剛才": 0.02, "刚才": 0.02, "さっき": 0.02,
               "剛剛": 0.02, "刚刚": 0.02, "兩天前": 2, "两天前": 2, "兩日前": 2, "三天前": 3,
               "這幾天": 3, "这几天": 3, "最近幾天": 3, "最近几天": 3}
#: 只是在問過去（沒有時間點）⇒ 被取代的舊值跟著同話題的現值一起出來
PAST_HINTS = ("之前", "以前", "上次", "那天", "那時", "當時", "前天", "昨天", "先週", "前回",
              "この前", "前に", "last time", "previously")

#: 長的先比：「一昨日」裡面也有「昨日」，短的先比會把兩天前當成一天前
_HINTS_LONGEST_FIRST = sorted(AS_OF_HINTS.items(), key=lambda item: -len(item[0]))
#: 「N 天前」這種帶數字的寫法（中英數都吃）
_N_DAYS = re.compile(r"(\d+)\s*(?:天|日|days?)\s*(?:前|ago)")
#: 年月日（2026-11-03／2026/11/3／2026年11月3日）
_YMD = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*[日号號]?")
#: 上個月／這個月 ＋ 幾號（上個月15号／先月15日／這個月3號／今月3日）
_REL_MONTH_DAY = re.compile(r"(上個月|上个月|先月|這個月|这个月|本月|今月)\s*(\d{1,2})\s*[日号號]")
#: 月日（11月3日／11月3号）
_MD = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号號]")
#: 斜線月日（11/3）——前後都不能再接數字或斜線，免得吃掉 2026/11/3 或分數
_MD_SLASH = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
_LAST_MONTH = {"上個月", "上个月", "先月"}


def _noon(year: int, month: int, day: int) -> float | None:
    """那一天的當地中午（時間戳）；日期不存在（2 月 30 日）回 None。"""
    try:
        stamp = time.mktime((year, month, day, 12, 0, 0, 0, 0, -1))
    except (OverflowError, ValueError):
        return None
    moment = time.localtime(stamp)
    if (moment.tm_year, moment.tm_mon, moment.tm_mday) != (year, month, day):
        return None                                    # mktime 會把 2/30 正規化成 3/2，那是寫錯的日期
    return stamp


def _month_day(month: int, day: int, now: float) -> float | None:
    """沒寫年份的月日：今年的那一天；若落在未來（今天之後）就當成去年。"""
    today = time.localtime(now)
    stamp = _noon(today.tm_year, month, day)
    if stamp is not None and (month, day) > (today.tm_mon, today.tm_mday):
        stamp = _noon(today.tm_year - 1, month, day)
    return stamp


def concrete_date(text: str, now: float | None = None) -> float | None:
    """問句裡有沒有**具體日期**；有就回那一天的當地中午，沒有回 None。"""
    body = str(text or "")
    moment = float(now if now is not None else time.time())
    match = _YMD.search(body)
    if match:
        return _noon(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = _REL_MONTH_DAY.search(body)
    if match:
        today = time.localtime(moment)
        year, month = today.tm_year, today.tm_mon
        if match.group(1) in _LAST_MONTH:
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        return _noon(year, month, int(match.group(2)))
    for pattern in (_MD, _MD_SLASH):
        match = pattern.search(body)
        if match:
            month, day = int(match.group(1)), int(match.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return _month_day(month, day, moment)
    return None


def time_intent(query: str, now: float | None = None) -> tuple[float | None, bool]:
    """回 `(as_of, wants_past)`：問題裡有沒有明確時間點／只是想問過去。"""
    body = str(query or "").lower()
    moment = float(now if now is not None else time.time())
    stamp = concrete_date(body, moment)
    if stamp is not None:
        return min(stamp, moment), True                # 今天的日期取中午可能還沒到 ⇒ 不超過現在
    match = _N_DAYS.search(body)
    if match:
        return moment - float(match.group(1)) * 86400.0, True
    for hint, days in _HINTS_LONGEST_FIRST:
        if hint.lower() in body:
            return moment - days * 86400.0, True
    return None, any(hint.lower() in body for hint in PAST_HINTS)


__all__ = ["AS_OF_HINTS", "PAST_HINTS", "concrete_date", "time_intent"]
