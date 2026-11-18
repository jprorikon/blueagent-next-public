"""YIELD 待辦的落盤：重啟後仍知道「她原本打算什麼時候補位」。"""

from __future__ import annotations

import json
import pathlib
import threading

from agent.brain.domain.pending import PendingIntervention


class PendingInterventionStore:
    def __init__(self, path=None) -> None:
        self._path = pathlib.Path(path) if path else None
        self._lock = threading.RLock()
        self._items: dict[str, PendingIntervention] = {}
        if self._path is not None:
            self._load()

    def add(self, pending: PendingIntervention) -> PendingIntervention:
        with self._lock:
            # 同一條線只保留最新的一個補位待辦
            self._items = {key: item for key, item in self._items.items()
                           if item.key != pending.key}
            self._items[pending.id] = pending
            self._save_locked()
            return pending

    def cancel_key(self, key: str) -> None:
        with self._lock:
            self._items = {pid: item for pid, item in self._items.items()
                           if item.key != key}
            self._save_locked()

    def remove(self, pending_id: str) -> None:
        with self._lock:
            self._items.pop(str(pending_id), None)
            self._save_locked()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._save_locked()

    def due(self, now: float) -> list[PendingIntervention]:
        with self._lock:
            return [item for item in self._items.values() if item.due_at <= now]

    def list_all(self) -> list[dict]:
        with self._lock:
            return [item.to_dict() for item in self._items.values()]

    def _load(self) -> None:
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return
        if not isinstance(rows, list):
            return
        for raw in rows:
            if isinstance(raw, dict):
                item = PendingIntervention.from_dict(raw)
                if item.key:
                    self._items[item.id] = item

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps([item.to_dict() for item in self._items.values()],
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass
