"""她的日記與工作成果（master 端「+」→ 日記；owner 2026-10-31）：`GET /api/diary`、`/api/diary/day`、`/api/diary/work`。

只有主人能看（同 `/api/mood`：那是她自己的內心與產出，guest 不該翻）。讀檔邏輯在 `app/diary.py`。

★ 2026-10-31：**索引與某一天的筆記分兩支**。生活迴圈一天寫 888–1082 步，14 天的正文一次全帶是 8 MB、
  七千多張卡片 ⇒ App 一開就卡死（owner 實測「頁面卡住、整個 app 卡死」）。現在索引只回「那天有幾步」，
  App 點開那一天才拿筆記，拿到就快取（每支都帶 `digest` ⇒ 可以帶 `If-None-Match` 問 304）。
"""

from __future__ import annotations

import hashlib
import json

from agent.brain.compose import diary as diary_reader
from agent.brain.compose.state import AppState

from agent.server.api.common import _owner_required


def _entity(state: AppState, query: dict) -> str:
    entity = str((query.get("entity") or ["mira"])[0] or "mira").strip()
    entities = getattr(state.app, "entities", None)
    if entities is not None and entities.get(entity) is None:
        return ""
    return entity


def _tag(payload: dict) -> dict:
    """把手上的內容算一個短雜湊塞進 `digest`（端末用它問 304；不算進雜湊本身）。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    payload["digest"] = hashlib.sha256(body).hexdigest()[:32]
    return payload


def get_diary(state: AppState, query: dict, *, auth=None) -> tuple[int, dict]:
    """`GET /api/diary?entity=&days=`：最近幾天的日記索引（不含每一步正文）＋成品清單。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity = _entity(state, query)
    if not entity:
        return 404, {"error": "unknown_entity"}
    try:
        days = int((query.get("days") or ["7"])[0])
    except (TypeError, ValueError):
        days = 7
    return 200, _tag(diary_reader.diary(state.app.data_dir, entity, days=days))


def get_diary_day(state: AppState, query: dict, *, auth=None) -> tuple[int, dict]:
    """`GET /api/diary/day?entity=&date=YYYY-MM-DD`：那一天的日記＋最近幾步筆記（正文截短）。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity = _entity(state, query)
    if not entity:
        return 404, {"error": "unknown_entity"}
    found = diary_reader.day(state.app.data_dir, entity, str((query.get("date") or [""])[0]))
    if found is None:
        return 404, {"error": "bad_date"}
    return 200, _tag(found)


def get_diary_work(state: AppState, query: dict, *, auth=None) -> tuple[int, dict]:
    """`GET /api/diary/work?entity=&name=`：一份成品的全文（名字只認成品資料夾裡真的有的檔）。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity = _entity(state, query)
    if not entity:
        return 404, {"error": "unknown_entity"}
    found = diary_reader.work(state.app.data_dir, entity, str((query.get("name") or [""])[0]))
    if found is None:
        return 404, {"error": "work_not_found"}
    return 200, found


__all__ = ["get_diary", "get_diary_day", "get_diary_work"]
