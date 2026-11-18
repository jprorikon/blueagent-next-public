"""她自己的任務台帳（master 端「看板＋日曆」；owner 2026-11-03）：

    GET /api/tasks?entity=&view=board|calendar&days=7

只有主人能看（同 `/api/diary`：那是她自己的安排，guest 不該翻）。
資料來源是 `data/tasks/<entity>.json`（`runtime/inner/task_ledger.py` 唯一讀寫入口，
她的 `tasks` MCP 工具改的是同一份檔）。

為什麼兩個 view 分開：看板看「狀態分佈」，日曆看「哪一天要做什麼」——
兩者一起回會讓 App 一次算兩張表（她一天可以排十幾件，畫面會卡）。
"""

from __future__ import annotations

import hashlib
import json

from agent.brain.compose.state import AppState

from agent.server.api.common import _owner_required


def _entity(state: AppState, query: dict) -> str:
    entity = str((query.get("entity") or ["mira"])[0] or "mira").strip()
    entities = getattr(state.app, "entities", None)
    if entities is not None and entities.get(entity) is None:
        return ""
    return entity


def _tag(payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    payload["digest"] = hashlib.sha256(body).hexdigest()[:32]
    return payload


def get_tasks(state: AppState, query: dict, *, auth=None) -> tuple[int, dict]:
    """`GET /api/tasks?entity=&view=&days=`：看板（按狀態）或日曆（按日子）。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity = _entity(state, query)
    if not entity:
        return 404, {"error": "unknown_entity"}
    ledgers = getattr(state.app, "ledgers", None) or {}
    ledger = ledgers.get(entity)
    if ledger is None:
        return 404, {"error": "no_ledger"}
    view = str((query.get("view") or ["board"])[0] or "board").strip().lower()
    try:
        days = int((query.get("days") or ["7"])[0])
    except (TypeError, ValueError):
        days = 7
    if view == "calendar":
        payload = {"entity": entity, "view": "calendar",
                   **ledger.calendar(days=max(1, min(90, days)))}
    else:
        payload = {"entity": entity, "view": "board", **ledger.board()}
    return 200, _tag(payload)


def post_tasks_comment(state: AppState, body: dict, *, auth=None) -> tuple[int, dict]:
    """`POST /api/tasks/comment`：主人在某一條任務上留一句批注（**不改狀態**）。

    owner 2026-11-03 定的兩種介入方式之一：批注＝她下次檢查時看到，怎麼處理她自己判斷；
    另一種是直接在對話裡要求她改（走她自己那套 MCP 工具，不經過這支）。
    """
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity = str(body.get("entity") or "").strip()
    if not entity:
        return 400, {"error": "missing_entity"}
    ledgers = getattr(state.app, "ledgers", None) or {}
    ledger = ledgers.get(entity)
    if ledger is None:
        return 404, {"error": "no_ledger"}
    task_id = str(body.get("id") or "").strip()
    text = str(body.get("text") or "").strip()
    if not task_id or not text:
        return 400, {"error": "missing_id_or_text"}
    if ledger.get(task_id) is None:
        return 404, {"error": "not_found"}
    by = str(getattr(auth, "principal", "") or "owner")
    row = ledger.comment(task_id, text, by=by)
    return 200, {"ok": True, "task": {"id": task_id,
                                      "comments": (row or {}).get("comments", [])}}


__all__ = ["get_tasks", "post_tasks_comment"]
