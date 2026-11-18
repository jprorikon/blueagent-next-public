"""TRPG 匯報的落點（owner 2026-10-22：「trpg 还是不要往 discord 里面投好一点」）。

預設**只落本地**：`data/logs/trpg-reports.jsonl`（一輪一行，帶桌／輪／席），
同時寫進她的私記（`activity_log`，private）——這樣下次對話她接得上「我昨天打到哪」。
要改投遞點只改 `BLUEAGENT_TRPG_REPORT_TARGET`（`local` 預設／`dm`／`discord`），
將來手機 app 通了就多一個 `app`。
"""

from __future__ import annotations

import json
import pathlib
import time


def report_path(data_dir) -> pathlib.Path:
    return pathlib.Path(data_dir) / "logs" / "trpg-reports.jsonl"


def append_report(data_dir, *, entity: str, text: str, table: str = "", seat: str = "",
                  round_no: int = 0, at: float | None = None) -> bool:
    """把一則匯報寫進本地檔；寫成功回 True（失敗不拋，匯報爛了不該斷打牌）。"""
    body = str(text or "").strip()
    if not body:
        return False
    path = report_path(data_dir)
    row = {"at": round(float(at if at is not None else time.time()), 3),
           "entity": str(entity), "table": str(table), "seat": str(seat),
           "round": int(round_no or 0), "text": body}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def tail(data_dir, limit: int = 10) -> list[dict]:
    """最近幾則匯報（給 `/api/state` 或排查用）。"""
    path = report_path(data_dir)
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-max(1, int(limit)):]:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return rows


__all__ = ["append_report", "report_path", "tail"]
