"""每日報（引擎內）：一天一次，她自己整理「今天」——不是外部 cron 替她報。

owner 2026-10-27：「定时报做成引擎内实现」。

做法＝**不新增服務**，複用既有的活動輪換：
  · 每個角色多一套 `daily-report` preset（`once_per_day=True`，`hours=(21, 0)`＝21:00–24:00）
  · `Rotation.pick()` 在時段內、且**今天還沒排過**時，優先挑它 ⇒ 她手上那件做完就接著報
  · 產物照既有管線：public 步驟進活動流／筆記／記憶，`on_public` 鉤子照樣可能觸發主動消息
  · 狀態落在 `life-rotation.json`（重啟不重報）

時段可由 `BLUEAGENT_REPORT_HOUR`（預設 21，`0`＝午夜）改；`BLUEAGENT_REPORT_HOUR=off` 可整條關掉。
"""

from __future__ import annotations

import os

REPORT_KEY = "daily-report"

#: 顯示名（活動流是「名字「標題」」，所以標題用日文名才讀得順）
NAMES = {"mira": "ミラ", "aster": "アスター", "nori": "ノリ"}


def report_hour(env=None) -> int | None:
    """回報告時段起始小時；`off`／負數 ⇒ None（關掉）。`0` 是午夜，不是關掉。"""
    raw = str((env or os.environ).get("BLUEAGENT_REPORT_HOUR") or "").strip().lower()
    if raw in {"off", "none", "no", "false"} or raw.startswith("-"):
        return None
    try:
        hour = int(raw) if raw else 21
    except ValueError:
        hour = 21
    return hour % 24 if 0 <= hour <= 23 else 21


def _window_hours(env) -> int:
    """報告視窗長度（小時，1–23）；寫壞就用 3（以前 `int()` 直接丟例外，活動庫整個建不起來）。"""
    try:
        return min(23, max(1, int(str(env.get("BLUEAGENT_REPORT_WINDOW_H") or 3).strip())))
    except ValueError:
        return 3


def report_preset(entity: str, *, name: str = "", hour: int | None = None):
    """那一套「今天的總結」。標題帶角色名字（活動流上看得出來是誰的報告）。"""
    from agent.brain.runtime.activity_log import PRIVATE, PUBLIC
    from agent.brain.runtime.life import Preset

    who = str(name or NAMES.get(str(entity), entity))
    start = 21 if hour is None else int(hour) % 24
    #: 視窗長度（小時）：她可能整個晚上都在做事，所以留「當天結束前都還算」的餘裕
    span = _window_hours(os.environ)
    return Preset(
        REPORT_KEY,
        f"{who}の今日のまとめ",
        "report",
        (
            ("今日の自分の記録を読み返す（activity.peek／notes.read）", PUBLIC),
            ("今日やったこと・分かったこと・今の気分を三つに書く", PUBLIC),
            ("明日の自分に一言だけ残す", PRIVATE),
        ),
        hours=(start, (start + span) % 24),
        once_per_day=True,
        brief=("これは**その日の報告**：①今日やったこと（事実）②分かったこと／進んだこと "
               "③今の気分。三つを短く、盛らない。手を動かしていないことは書かない。"),
    )
