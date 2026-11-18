"""用「跳」記時間的預約（2026-10-27）。

owner 要的是「她知道**幾跳之後**要提某件事」。心跳是她自己的節拍（30–75 秒一跳，
arousal 會調），所以「跳」比「秒」更像她自己的時間單位：

    remind.in(after_ticks=20, note="問他晚饭吃了没")   → 20 跳後到期
    remind.in(after_minutes=90, note="…")              → 牆上時間（換算的跳數只拿來顯示）
    remind.in(at="22:30", note="…")                    → 絕對時刻（寫錯 ⇒ ValueError，不寫入）

到期由心跳檢查（`Heartbeat.tick`）：把到期的那幾句塞進她這一跳的思考／台詞，
**真的開口之後**才標記 fired（只講一次）。這一跳沒開口（不想說／被閘擋）就留到下一跳——
以前是先標再判斷，而大多數跳都不開口 ⇒ 預約多半一聲不響地作廢（2026-10-27 審查）。

檔案：`data/reminders/<entity>.json`（一個角色一份，很小）。純檔案 I/O，沒有背景執行緒。
"""

from __future__ import annotations

import json
import pathlib
import threading
import time

DEFAULT_TICKS = 20


def _parse_time(value: str, *, now: float) -> float:
    """`HH:MM` → 下一個那個時刻（今天過了就算明天）。認不出來回 0。"""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        hour, minute = (int(part) for part in text.replace("：", ":").split(":")[:2])
    except (TypeError, ValueError):
        return 0.0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return 0.0
    local = time.localtime(now)
    target = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, hour, minute, 0, 0, 0, -1))
    if target <= now:
        target += 86400
    return target


class ReminderStore:
    def __init__(self, root, *, clock=time.time) -> None:
        self.root = pathlib.Path(root)
        self._clock = clock
        #: 讀改寫要整段鎖住：工具（回話執行緒）在加、心跳執行緒在標 fired，
        #: 交錯時會互相蓋掉（新的那筆不見，或已講過的又變回沒講）。
        self._lock = threading.RLock()

    def path_for(self, entity: str) -> pathlib.Path:
        return self.root / f"{entity}.json"

    def items(self, entity: str) -> list[dict]:
        path = self.path_for(entity)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return []
        rows = data.get("items") if isinstance(data, dict) else data
        return [row for row in (rows or ()) if isinstance(row, dict)]

    def _write(self, entity: str, rows: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(entity)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def add(self, entity: str, text: str, *, tick: int = 0, after_ticks=None,
            after_minutes=None, at_time: str = "", interval_s: float = 0.0,
            now: float | None = None) -> dict:
        moment = float(now if now is not None else self._clock())
        body = str(text or "").strip()
        if not body:
            raise ValueError("note required")
        at_tick = 0
        at_stamp = 0.0
        est_tick = 0
        if after_ticks not in (None, ""):
            at_tick = int(tick) + max(1, int(after_ticks))
        elif after_minutes not in (None, ""):
            # 「N 分後」只認牆上時間：心跳間隔會隨 arousal／精力變，用建立當下的間隔換算的跳數
            # 不能拿來觸發（75 秒換成 8 跳，之後變 30 秒一跳 ⇒ 240 秒就響，review 2026-10-28）。
            minutes = max(1.0, float(after_minutes))
            at_stamp = moment + minutes * 60.0
            if interval_s > 0:
                est_tick = int(tick) + max(1, int(minutes * 60.0 // float(interval_s)))
        elif at_time:
            at_stamp = _parse_time(at_time, now=moment)
            if not at_stamp:
                # 以前寫成 at=0、at_tick=0 的一筆 ⇒ 永遠不到期，工具卻回成功
                raise ValueError(f"bad time: {at_time!r}（HH:MM、24 時間制）")
        else:
            at_tick = int(tick) + DEFAULT_TICKS
        record = {"text": body, "created_at": round(moment, 3),
                  "at_tick": at_tick or 0, "at": round(at_stamp, 3), "fired_at": 0.0}
        if est_tick:
            record["est_tick"] = est_tick                 # 只給「大約第幾拍」顯示用
        with self._lock:
            rows = self.items(entity)
            rows.append(record)
            self._write(entity, rows)
        return record

    def pending(self, entity: str, *, tick: int, now: float | None = None) -> list[dict]:
        moment = float(now if now is not None else self._clock())
        out = []
        for row in self.items(entity):
            if row.get("fired_at"):
                continue
            if _due(row, tick=int(tick), now=moment):
                continue
            out.append(row)
        return out

    def due(self, entity: str, *, tick: int, now: float | None = None) -> list[dict]:
        """到期、還沒講過的那幾句（**只看不標**；講出口之後再 `mark_fired`）。"""
        moment = float(now if now is not None else self._clock())
        return [row for row in self.items(entity) if not row.get("fired_at")
                and _due(row, tick=int(tick), now=moment)]

    def mark_fired(self, entity: str, rows, *, now: float | None = None) -> int:
        """把 `due()` 拿到的那幾句標成已講（按 `created_at`＋`text` 對回檔案裡那一列）。"""
        moment = float(now if now is not None else self._clock())
        keys = {(row.get("created_at"), row.get("text")) for row in rows or ()}
        if not keys:
            return 0
        with self._lock:
            stored = self.items(entity)
            marked = 0
            for row in stored:
                if not row.get("fired_at") and (row.get("created_at"), row.get("text")) in keys:
                    row["fired_at"] = round(moment, 3)
                    marked += 1
            if marked:
                self._write(entity, stored)
        return marked

    def take_due(self, entity: str, *, tick: int, now: float | None = None) -> list[dict]:
        """到期的那幾句（標記 fired 並落檔）。"""
        due = self.due(entity, tick=tick, now=now)
        self.mark_fired(entity, due, now=now)
        return due


def _due(row: dict, *, tick: int, now: float) -> bool:
    """有牆上時刻就**只看時刻**（舊檔裡分鐘預約兩個欄位都寫了，也照這條）；否則看跳號。"""
    stamp = float(row.get("at") or 0.0)
    if stamp:
        return now >= stamp
    return bool(int(row.get("at_tick") or 0) and int(tick) >= int(row["at_tick"]))


__all__ = ["DEFAULT_TICKS", "ReminderStore"]
