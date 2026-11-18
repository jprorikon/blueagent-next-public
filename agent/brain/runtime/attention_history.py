"""注意力决策历史：從 JSONL 聚合出按日/按群/按動作的看板數據。"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from collections import Counter, defaultdict

from agent.brain.runtime.ops.rotation import DailyGuard, RotationGuard


class AttentionHistory:
    def __init__(self, path=None, *, max_rows: int = 5000) -> None:
        self._path = pathlib.Path(path) if path else None
        self._max_rows = max(100, int(max_rows))
        self._lock = threading.RLock()
        self._guard = RotationGuard(self._path) if self._path is not None else None
        # 分級輪轉：按天切進 `daily/attention-YYYYMMDD.jsonl`，保留 14 天（owner 2026-10-22）
        self._daily = DailyGuard(self._path, kind="attention") \
            if self._path is not None else None
        self._total = 0
        self._shadow = 0
        self._actions: Counter = Counter()
        self._days: dict[str, Counter] = defaultdict(Counter)
        self._groups: dict[str, Counter] = defaultdict(Counter)
        if self._path is not None:
            self._load()

    def record(self, row: dict) -> None:
        with self._lock:
            self._ingest(row)
            if self._path is not None:
                try:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    with self._path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                    if self._guard is not None:
                        self._guard.note_write()
                    if self._daily is not None:
                        self._daily.note_write()
                except OSError:
                    pass

    def summary(self) -> dict:
        with self._lock:
            days = []
            for day in sorted(self._days)[-7:]:
                counts = dict(self._days[day])
                days.append({"day": day, "counts": counts,
                             "total": sum(counts.values())})
            groups = []
            for group, counter in sorted(self._groups.items(),
                                         key=lambda pair: -sum(pair[1].values()))[:5]:
                counts = dict(counter)
                groups.append({"group": group, "counts": counts,
                               "total": sum(counts.values())})
            return {"total": self._total, "shadow": self._shadow,
                    "by_action": dict(self._actions), "days": days,
                    "groups": groups}

    def _ingest(self, row: dict) -> None:
        action = str(row.get("actual") or row.get("proposed") or "unknown")
        group = str(row.get("scope") or row.get("source") or "unknown")
        day = time.strftime("%Y-%m-%d", time.localtime(float(row.get("at") or time.time())))
        self._total += 1
        self._shadow += 1 if row.get("shadow") else 0
        self._actions[action] += 1
        self._days[day][action] += 1
        self._groups[group][action] += 1

    def _load(self) -> None:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()[-self._max_rows:]
        except (FileNotFoundError, OSError):
            return
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                self._ingest(row)
