"""位置（座標）倉：**按端末**存，天氣查詢取「最近上報的那台」。

為什麼按端末而不是按人（owner 2026-10-03 定的口徑，沿用）：天氣問的是「**你現在在哪**」，
同一個人在手機與 Discord 兩處會給出不同的真實位置；按人存會讓兩邊互相蓋掉。
端末只是感測器（跟麥克風同一個地位），不取得任何工具權。

檔案：`data/locations/<device>.json`

    {"device": "phone", "principal": "owner", "lat": 35.68, "lon": 139.76,
     "accuracy": 120.0, "at": 1792730213.28, "source": "gps"}

`latest()` 回**全機最近一次**上報（跨端末），這就是天氣預設用的位置。
"""

from __future__ import annotations

import json
import pathlib
import threading
import time


class LocationStore:
    def __init__(self, root) -> None:
        self.root = pathlib.Path(root)
        self._lock = threading.RLock()

    def _path(self, device: str) -> pathlib.Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(device or "unknown"))
        return self.root / f"{safe or 'unknown'}.json"

    def save(self, device: str, lat: float, lon: float, *, principal: str = "",
             accuracy: float | None = None, source: str = "gps",
             at: float | None = None) -> dict:
        row = {"device": str(device or "unknown"), "principal": str(principal or ""),
               "lat": float(lat), "lon": float(lon),
               "accuracy": None if accuracy is None else float(accuracy),
               "at": float(time.time() if at is None else at),
               "source": str(source or "")}
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(device)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        return row

    def get(self, device: str) -> dict | None:
        try:
            row = json.loads(self._path(device).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return row if isinstance(row, dict) else None

    def latest(self, *, principal: str = "", entity: str = "") -> dict | None:
        """全機最近一次上報；entity 仅用于兼容平台组合仓，旧仓不按角色分片。"""
        best: dict | None = None
        who = str(principal or "")
        if not self.root.exists():
            return None
        for path in self.root.glob("*.json"):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(row, dict) or (who and str(row.get("principal") or "") != who):
                continue
            if best is None or float(row.get("at") or 0.0) > float(best.get("at") or 0.0):
                best = row
        return best

    def snapshot(self) -> dict:
        rows = []
        if self.root.exists():
            for path in sorted(self.root.glob("*.json")):
                try:
                    rows.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return {"count": len(rows), "locations": rows}
