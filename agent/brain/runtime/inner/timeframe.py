"""時間感：給她的 prompt 一隻鐘（2026-10-27）。

owner 回報「她的時間概念很模糊」：查了才發現 prompt 裡**一個時間字都沒有**——
歷史訊息不帶時間、召回的事實不帶時間、她自己的私記也不帶；心跳有 `ticks` 卻從不進 prompt。
所以她分不出「剛剛」和「三小時前」，也說不出「這是幾輪之前的事」。

這一層只做兩件事（純函式＋一個聚合器，不碰任何狀態）：

  · `human_age()`：秒 → 「さっき／3 分前／2 時間前／昨日 23:10／10月25日 23:10」；
  · `Timeline.block()`：把「現在幾點、這條線第幾輪、上一句多久前、上次封片多久前、
    今天活動幾件、心跳跳了幾次」壓成一小段（≤ 6 行）。

心跳那半邊（`N 跳之後提醒我`）在 `runtime/inner/reminders.py`。
"""

from __future__ import annotations

import time

from agent.brain.domain.time import human_age

_WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")

#: 來源前綴 → 端的人話（`discord:vc` 要排在 `discord` 前面）。心跳組句也用這張（`heartbeat_compose`）。
CHANNELS: tuple[tuple[str, str], ...] = (
    ("discord:vc", "Discord通話"), ("discord", "Discord"), ("phone:call", "アプリ通話"),
    ("mr", "MR"), ("app", "アプリ"), ("phone", "アプリ"), ("api", "アプリ"))


def channel_label(source: str) -> str:
    """`discord:123`／`app:cnv_…`／`phone:owner` → 「Discord」「アプリ」…；認不出回空字串。"""
    body = str(source or "")
    for prefix, name in CHANNELS:
        if body.startswith(prefix):
            return name
    return ""


def channel_line(thread) -> str:
    """同一條線、同一個人，這一句和上一句**不在同一個端** ⇒ 一行提醒（common-ground H3）。

    一條線橫跨 App／Discord／通話；他在 Discord 說的事，在 App 的畫面裡翻不到。
    提到上一個端的事時要帶上「どこで」，不能用「さっき」直接指。同一個端／認不出 ⇒ 空字串。
    """
    turns = [turn for turn in list(getattr(thread, "transcript", ()) or ())
             if str(getattr(turn, "role", "")) == "user"]
    if len(turns) < 2:
        return ""
    current = turns[-1]
    who = str(getattr(current, "speaker", "") or "")
    previous = next((turn for turn in reversed(turns[:-1])
                     if str(getattr(turn, "speaker", "") or "") == who), None)
    if previous is None:
        return ""
    now_end = channel_label(getattr(current, "source", ""))
    before = channel_label(getattr(previous, "source", ""))
    if not now_end or not before or now_end == before:
        return ""
    return (f"- 相手はいま {now_end} から話している（その前のひとことは {before}）。"
            f"{before} で話したことは {now_end} の画面には出ていない。"
            f"触れるなら「{before}で話してた〜」と場所を添える")


def stamp(at: float, *, now: float) -> str:
    """歷史訊息前面那個小標記；沒有時間就回空字串。"""
    if not at:
        return ""
    return f"（{human_age(float(now) - float(at), now=now)}）"


def clock_line(now: float) -> str:
    moment = time.localtime(now)
    return time.strftime("%Y-%m-%d", moment) + f"（{_WEEKDAYS[moment.tm_wday]}）" + time.strftime("%H:%M", moment)


class Timeline:
    """把「她身邊的時鐘」聚起來。`archive` / `heartbeats` 由 bootstrap 晚一步補上。"""

    def __init__(self, *, archive=None, heartbeats=None, activity_log=None,
                 clock=time.time) -> None:
        self.archive = archive
        self.heartbeats = heartbeats
        self.activity_log = activity_log
        self._clock = clock

    def block(self, entity: str, thread=None, *, now: float | None = None) -> str:
        """回一段 ≤ 6 行的「時間感」；什麼都沒有就回空字串（不硬塞）。"""
        moment = float(now if now is not None else self._clock())
        lines: list[str] = [f"- 現在：{clock_line(moment)}"]
        if thread is not None:
            turns = int(getattr(thread, "turns", 0) or 0)
            last = float(getattr(thread, "last_active_at", 0.0) or 0.0)
            head = [f"{turns + 1} 往復目"]
            if last:
                head.append(f"前の発言は{human_age(moment - last, now=moment)}")
            lines.append("- この会話：" + "、".join(head))
            moved = channel_line(thread)
            if moved:
                lines.append(moved)
        sealed = self._last_seal(entity, moment)
        if sealed:
            lines.append(f"- 前の会話のまとまり（エピソード）が終わったのは：{sealed}")
        activity = self._today_activity(entity, moment)
        if activity is not None:
            lines.append(f"- 今日（00:00 から）自分で {activity} 件やった")
        beat = self._beat(entity)
        if beat:
            line = (f"- 鼓動：{beat['ticks']} 拍目（1 拍およそ {int(beat['interval_s'])} 秒）")
            if beat["since_spoken"]:
                line += f"、自分から最後に話しかけてから {beat['since_spoken']}"
            lines.append(line)
        return "### いま（時間感）\n" + "\n".join(lines) if lines else ""

    # ── 內部 ────────────────────────────────────────────────────────────────
    def _last_seal(self, entity: str, now: float) -> str:
        if self.archive is None:
            return ""
        try:
            latest = self.archive.latest(50)
        except Exception:                                  # noqa: BLE001 - 時間感不該擋回話
            return ""
        # 只看**她自己的**那一集：以前取全域最後一集，別的角色剛封片也會算進來
        mine = [ep for ep in latest if str(getattr(ep, "entity", "")) == str(entity)]
        ended = float(getattr(mine[-1], "ended_at", 0.0) or 0.0) if mine else 0.0
        if not ended:
            return ""
        return human_age(now - ended, now=now)

    def _today_activity(self, entity: str, now: float) -> int | None:
        if self.activity_log is None:
            return None
        try:
            rows = self.activity_log.entries(entity, limit=200)
        except Exception:                                  # noqa: BLE001
            return None
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        return sum(1 for row in rows if float(row.get("at") or 0.0) >= midnight)

    def _beat(self, entity: str) -> dict | None:
        if self.heartbeats is None:
            return None
        try:
            beat = self.heartbeats.get(entity)
        except Exception:                                  # noqa: BLE001
            return None
        if beat is None:
            return None
        snapshot = beat.snapshot() if hasattr(beat, "snapshot") else {}
        ticks = int(snapshot.get("ticks") or 0)
        interval = float(snapshot.get("interval_s") or 0.0)
        if ticks <= 0 or interval <= 0:
            return None
        # 「距上次開口」＝現在的跳號 − 上次開口那一跳（`Heartbeat.last_spoken_tick`）。
        # 以前用「總跳數 − 開口次數」，那不是距離（500 跳裡開口 10 次 ⇒ 寫成「490 跳前」）。
        last = int(snapshot.get("last_spoken_tick") or 0)
        since = ""
        if 0 < last <= ticks:
            gap = ticks - last
            since = f"{gap} 拍（約 {int(gap * interval // 60)} 分）"
        return {"ticks": ticks, "interval_s": interval, "since_spoken": since}


__all__ = ["Timeline", "clock_line", "human_age", "stamp"]
