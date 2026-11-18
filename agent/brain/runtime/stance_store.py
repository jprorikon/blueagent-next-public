"""立场的落盘：**每实体一个 JSON**，里面按 principal 分槽。

键是 `(entity, principal)`，不是 `(device, character)`。同一角色在手机、Discord、
TRPG 桌上遇到同一个人，读的是同一份立场；换设备不会换一个人。
"""

from __future__ import annotations

import json
import pathlib
import threading

from agent.brain.domain.ids import norm
from agent.brain.domain.stance import Stance


class StanceStore:
    def __init__(self, root: pathlib.Path | str) -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, entity: str) -> pathlib.Path:
        return self.root / f"{norm(entity, fallback='default')}.json"

    def load(self, entity: str) -> dict[str, Stance]:
        with self._lock:
            return self._load_locked(entity)

    def _load_locked(self, entity: str) -> dict[str, Stance]:
        path = self.path(entity)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:                              # noqa: BLE001 - 坏档不该挡她起来
            return {}
        if not isinstance(raw, dict):
            return {}
        rows = raw.get("principals")
        if not isinstance(rows, dict):
            return {}
        out: dict[str, Stance] = {}
        for principal, row in rows.items():
            stance = Stance.from_dict(row if isinstance(row, dict) else None)
            if stance is None:
                continue
            key = norm(principal, fallback=stance.principal)
            out[key] = Stance(
                entity=entity, principal=key, axes=stance.axes, trust=stance.trust,
                updated_at=stance.updated_at, revision=stance.revision)
        return out

    def get(self, entity: str, principal: str) -> Stance | None:
        return self.load(entity).get(norm(principal, fallback=""))

    def snapshot(self, entity: str) -> list[Stance]:
        rows = list(self.load(entity).values())
        return sorted(rows, key=lambda stance: stance.principal)

    def put(self, stance: Stance) -> Stance:
        with self._lock:
            rows = self._load_locked(stance.entity)
            rows[stance.principal] = stance
            self._save_locked(stance.entity, rows)
        return stance

    def forget(self, entity: str, principal: str) -> bool:
        with self._lock:
            rows = self._load_locked(entity)
            if rows.pop(norm(principal, fallback=""), None) is None:
                return False
            self._save_locked(entity, rows)
            return True

    def _save_locked(self, entity: str, rows: dict[str, Stance]) -> None:
        path = self.path(entity)
        body = {
            "entity": norm(entity, fallback="default"),
            "principals": {
                principal: stance.to_dict()
                for principal, stance in sorted(rows.items())
            },
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)


__all__ = ["StanceStore"]
