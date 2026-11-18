"""每個角色自己的心跳節拍（秒）——從 `runtime/heartbeat.py` 搬出來（本檔已到 600 行上限）。"""

from __future__ import annotations

import os

#: 每個角色自己的節拍（秒）。可用 `BLUEAGENT_HEARTBEAT_S_<ENTITY>` 覆寫。
DEFAULT_INTERVALS = {"mira": 30.0, "aster": 45.0, "nori": 60.0}


def interval_for(entity: str, default: float = 30.0) -> float:
    raw = os.environ.get(f"BLUEAGENT_HEARTBEAT_S_{str(entity).upper()}")
    if raw:
        try:
            return max(5.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_INTERVALS.get(str(entity), float(default))


__all__ = ["DEFAULT_INTERVALS", "interval_for"]
