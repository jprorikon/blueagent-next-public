"""活動檢查點的落盤：每實體一個 JSON，原子寫。

她不需要資料庫才能「像個人」——需要的只是：每次狀態變更後，把「我走到哪」可靠地寫下去。
"""

from __future__ import annotations

import json
import pathlib

from agent.brain.domain.activity import Activity
from agent.brain.domain.ids import norm


class ActivityStore:
    def __init__(self, root: pathlib.Path | str) -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, entity: str) -> pathlib.Path:
        return self.root / f"{norm(entity, fallback='default')}.json"

    def load(self, entity: str) -> Activity | None:
        path = self.path(entity)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:                              # noqa: BLE001 - 壞檔不該擋她起來
            return None
        if not isinstance(raw, dict):
            return None
        return Activity.from_dict(raw)

    def save(self, activity: Activity) -> None:
        path = self.path(activity.entity)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(activity.to_dict(), ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(path)
        except Exception:                              # noqa: BLE001 - 寫不進去時至少別炸主流程
            pass

    def clear(self, entity: str) -> None:
        try:
            self.path(entity).unlink()
        except FileNotFoundError:
            pass
        except Exception:                              # noqa: BLE001
            pass
