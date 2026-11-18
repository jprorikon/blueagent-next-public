"""心跳留痕（2026-10-27）：每一跳寫一行，讓她（和我們）能回指「第 N 跳」。

為什麼要：`Heartbeat.ticks` 只是記憶體裡的計數器（開機時用 `resume()` 從這裡接回）；而且事後查不到
「她第 120 跳的時候在做什麼」。留痕之後 `reminders`（幾跳後提醒）與日後的檢索
才有共同的時間軸。

檔案：`data/beats/<entity>.jsonl`，一行 `{"tick": 120, "at": 1793..., "interval_s": 30.0}`。
大小到 `max_bytes` 就只留後半（心跳檔只是旁證，不需要全史）。
"""

from __future__ import annotations

import json
import pathlib

DEFAULT_MAX_BYTES = 2 * 1024 * 1024


class BeatLog:
    def __init__(self, root, *, max_bytes: int = DEFAULT_MAX_BYTES, clock=None) -> None:
        self.root = pathlib.Path(root)
        self.max_bytes = int(max_bytes)

    def path_for(self, entity: str) -> pathlib.Path:
        return self.root / f"{entity}.jsonl"

    def mark(self, entity: str, *, tick: int, at: float, interval_s: float,
             spoke: bool = False) -> None:
        """一跳一行；`spoke=True`＝這一跳她開口了（同一跳會再多一行，給「距上次開口」用）。"""
        path = self.path_for(entity)
        row = {"tick": int(tick), "at": round(float(at), 3),
               "interval_s": round(float(interval_s), 1)}
        if spoke:
            row["spoke"] = True
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            if path.stat().st_size > self.max_bytes:
                lines = path.read_text(encoding="utf-8").splitlines()
                path.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
        except OSError:
            return                          # 留痕失敗不該擋心跳

    def tail(self, entity: str, limit: int = 20) -> list[dict]:
        path = self.path_for(entity)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return []
        rows = []
        for line in lines[-max(1, int(limit)):]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def resume(self, entity: str, *, limit: int = 500) -> tuple[int, int]:
        """重啟接續：回 `(最後一跳, 最後開口那一跳)`；查不到就是 0。

        `Heartbeat.ticks` 只在記憶體裡，重啟歸零 ⇒「N 跳後提醒」會被推遲、留痕的跳號會重複
        （2026-10-27 審查）。開機時從這裡接回去。
        """
        last = spoke = 0
        for row in self.tail(entity, limit=limit):
            tick = int(row.get("tick") or 0)
            last = max(last, tick)
            if row.get("spoke"):
                spoke = max(spoke, tick)
        return last, spoke

    def tick_at(self, entity: str, moment: float) -> int:
        """某個時刻大約是第幾跳（給「N 跳前」用）；查不到回 0。"""
        rows = self.tail(entity, limit=500)
        best = 0
        for row in rows:
            if float(row.get("at") or 0.0) <= moment:
                best = max(best, int(row.get("tick") or 0))
        return best


__all__ = ["BeatLog", "DEFAULT_MAX_BYTES"]
