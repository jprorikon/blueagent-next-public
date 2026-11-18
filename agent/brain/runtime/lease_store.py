"""FocusLease 的持久化：重啟後 A 的追問仍然接得上。"""

from __future__ import annotations

import json
import pathlib
import threading

from agent.brain.domain.attention import AttentionConfig, FocusLease
from agent.brain.domain.ids import norm


class FocusLeaseStore:
    def __init__(self, path=None) -> None:
        self._path = pathlib.Path(path) if path else None
        self._lock = threading.RLock()
        self._leases: dict[tuple[str, str], FocusLease] = {}
        if self._path is not None:
            self._load()

    def grant(self, *, entity: str, thread_id: str, principal: str,
              group: str, now: float, config: AttentionConfig) -> FocusLease:
        with self._lock:
            lease = FocusLease(entity=norm(entity), thread_id=thread_id,
                               principal=norm(principal), group=group,
                               created_at=now, expires_at=now + config.lease_ttl_s,
                               turns_left=config.lease_turns)
            self._leases[(thread_id, lease.principal)] = lease
            self._save_locked()
            return lease

    def match(self, thread_id: str, principal: str, now: float) -> FocusLease | None:
        with self._lock:
            lease = self._leases.get((thread_id, norm(principal)))
            if lease is None:
                return None
            if not lease.active(now):
                self._leases.pop((thread_id, lease.principal), None)
                self._save_locked()
                return None
            return lease

    def consume(self, lease: FocusLease) -> None:
        with self._lock:
            lease.turns_left -= 1
            if lease.turns_left <= 0:
                self._leases.pop((lease.thread_id, lease.principal), None)
            self._save_locked()

    def revoke(self, thread_id: str, principal: str = "") -> None:
        with self._lock:
            if principal:
                self._leases.pop((thread_id, norm(principal)), None)
            else:
                for key in [key for key in self._leases if key[0] == thread_id]:
                    self._leases.pop(key, None)
            self._save_locked()

    def snapshot(self, now: float) -> list[dict]:
        with self._lock:
            return [lease.to_dict() for lease in self._leases.values()
                    if lease.active(now)]

    def _load(self) -> None:
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return
        if not isinstance(rows, list):
            return
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            lease = FocusLease.from_dict(raw)
            if lease.thread_id and lease.principal:
                self._leases[(lease.thread_id, lease.principal)] = lease

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps([lease.to_dict() for lease in self._leases.values()],
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass
