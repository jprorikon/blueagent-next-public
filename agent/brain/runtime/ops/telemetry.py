"""每輪的結構化紀錄（`data/logs/turns.jsonl`）＋ `/api/metrics`。

為什麼需要：現在「這一輪多久」「TTS 有沒有失敗」「哪個角色在回」只能翻 stdout，
而且容器重啟就沒了。這裡把**一輪一行**寫成 JSONL（append-only），並在記憶體留一個環形緩衝
算 p50／p95——看板與排障都靠它，不需要另外跑 Prometheus。

刻意不做的事：
  · 不記使用者原文（記憶已經有；日誌裡只留長度與 thread id）——日誌被看到不等於對話被看到。
  · 不記 token（指紋都不記）。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from collections import deque

from agent.brain.runtime.ops.rotation import DailyGuard, RotationGuard, usage

#: 記憶體裡保留多少輪（算百分位用；不影響檔案）。
_RING = 200


class TurnRecorder:
    def __init__(self, path: pathlib.Path | str | None = None, *, ring: int = _RING,
                 data_root: pathlib.Path | str | None = None,
                 max_bytes: int | None = None, keep: int | None = None) -> None:
        # 倉庫根＝這支往上四層（runtime/ops/telemetry.py ⇒ ops → runtime → brain → agent → 根）。
        # 2026-11-16 搬進 ops/ 之後層數多一層：用 parents[3] 會落到 agent/data/logs。
        default = pathlib.Path(os.environ.get("BLUEAGENT_LOG_DIR")
                               or (pathlib.Path(__file__).resolve().parents[4] / "data" / "logs"))
        self.path = pathlib.Path(path) if path is not None else default / "turns.jsonl"
        #: 同一個目錄的上限（`turns.jsonl` 自己一組；activity 各檔各一組）。
        self.guard = RotationGuard(self.path, max_bytes=max_bytes, keep=keep)
        #: 分級輪轉：每輪紀錄按天切、保留 30 天
        self.daily = DailyGuard(self.path, kind="turns")
        #: `data/` 根（`/api/metrics` 的 `data_bytes` 用）；不給就算不出來。
        self.data_root = pathlib.Path(data_root) if data_root is not None else self.path.parent.parent
        self._lock = threading.Lock()
        self._rows: deque[dict] = deque(maxlen=max(10, int(ring)))
        self.count = 0
        self.failures = 0

    def record(self, **fields) -> dict:
        row = {"at": round(time.time(), 3), **fields}
        with self._lock:
            self._rows.append(row)
            self.count += 1
            if row.get("error"):
                self.failures += 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass                                  # 寫不進去不能影響對話
        else:
            self.guard.note_write()               # 每 N 行才 stat 一次
            self.daily.note_write()               # 跨日就切檔（按天分級）
        return row

    # ── 統計 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
        return float(ordered[index])

    def daily_summaries(self) -> dict:
        """各檔的日檔概況（`/api/metrics`）：幾個檔、最舊／新、保留幾天。"""
        out: dict[str, dict] = {}
        guard = getattr(self, "daily", None)
        if guard is not None:
            out["turns"] = guard.summary()
        return out

    def metrics(self) -> dict:
        with self._lock:
            rows = list(self._rows)
            total, failures = self.count, self.failures
        latency = [float(row.get("ms") or 0) for row in rows if row.get("ms")]
        first = [float(row.get("first_audio_ms") or 0) for row in rows
                 if row.get("first_audio_ms")]
        by_entity: dict[str, int] = {}
        by_engage: dict[str, int] = {}
        speech_errors = 0
        for row in rows:
            by_entity[str(row.get("entity") or "?")] = by_entity.get(
                str(row.get("entity") or "?"), 0) + 1
            by_engage[str(row.get("engage") or "?")] = by_engage.get(
                str(row.get("engage") or "?"), 0) + 1
            speech_errors += int(row.get("speech_errors") or 0)
        return {
            "turns": total,
            "failures": failures,
            # 工具分層與動作的成效（owner 2026-10-31）：上線後看這兩塊決定要不要調
            "tool_tiers": self._tool_tiers(rows),
            "motion": self._motion(rows),
            "window": len(rows),
            "latency_ms": {"p50": self._percentile(latency, 50),
                           "p95": self._percentile(latency, 95),
                           "max": max(latency) if latency else 0.0},
            "first_audio_ms": {"p50": self._percentile(first, 50),
                               "p95": self._percentile(first, 95)},
            "speech_errors": speech_errors,
            "by_entity": by_entity,
            "by_engage": by_engage,
            "log": str(self.path),
            # 資料治理：`/api/metrics` 一眼看到「多大、轉了幾次」
            "rotation": {"max_bytes": self.guard.max_bytes, "keep": self.guard.keep,
                         "rotations": self.guard.rotations},
            "data_bytes": self._usage(),
        }

    @staticmethod
    def _tool_tiers(rows: list[dict]) -> dict:
        """一開始給了幾支工具／多少字（平均）、多少輪真的解鎖了按需工具、解鎖了哪些。

        判讀：`unlock_rate` 很低但常被問天氣／網頁 ⇒ 她沒先 `skill.view`（要加強引導或放回常駐）；
        某支工具解鎖次數很高 ⇒ 考慮放回常駐，省一次往返。
        """
        measured = [row for row in rows if row.get("tools") is not None]
        if not measured:
            return {"turns": 0}
        unlocked: dict[str, int] = {}
        for row in measured:
            for name in row.get("tools_unlocked") or ():
                unlocked[str(name)] = unlocked.get(str(name), 0) + 1
        with_unlock = sum(1 for row in measured if row.get("tools_unlocked"))
        return {
            "turns": len(measured),
            "tools_avg": round(sum(float(row.get("tools") or 0) for row in measured) / len(measured), 1),
            "tool_chars_avg": round(sum(float(row.get("tool_chars") or 0) for row in measured) / len(measured)),
            "unlock_rate": round(with_unlock / len(measured), 3),
            "unlocked": dict(sorted(unlocked.items(), key=lambda item: -item[1])),
        }

    @staticmethod
    def _motion(rows: list[dict]) -> dict:
        """動作幀：她自己標的（`chosen`）與規則表挑的（`rule`）各幾次、哪些 tag。"""
        out: dict = {"chosen": 0, "rule": 0, "tags": {}}
        for row in rows:
            value = str(row.get("motion") or "")
            how, _, tag = value.partition(":")
            if how not in ("chosen", "rule") or not tag:
                continue
            out[how] += 1
            out["tags"][tag] = out["tags"].get(tag, 0) + 1
        return out

    def _usage(self) -> dict:
        """`data/` 各項大小（每次呼叫都重新算；`/api/metrics` 不是熱路徑）。"""
        if not self.data_root.is_dir():
            return {}
        items = usage(self.data_root)
        return {"total": sum(item["bytes"] for item in items.values()), "items": items}

    def tail(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._rows)[-max(1, int(limit)):]


__all__ = ["TurnRecorder"]
