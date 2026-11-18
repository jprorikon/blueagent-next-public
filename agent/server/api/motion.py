"""動作片段（`bamotion/1`）：`GET /api/motion/<id>`、`GET /api/motion?tag=`。

規格見 `docs/PLAN-motion-kimodo-20261031.md` §5.5：要登入（同表情包）；id 不存在 ⇒ 404；
HTTP 層拿片段 id 當 ETag（端末取過就不重下載）。**這裡不生成**任何動作——生成在 GPU 機。
"""

from __future__ import annotations

from agent.brain.compose.state import AppState

from agent.server.api.common import _forbidden, _unauthorized


def _library(state: AppState):
    return getattr(state.app, "motion", None)


def get_motion(state: AppState, clip_id: str, *, auth=None) -> tuple[int, dict]:
    """`GET /api/motion/<id>` ⇒ `(status, 片段)`；庫裡沒有這個 id ⇒ 404（不是 500）。"""
    if auth is None:
        return _unauthorized()
    if not auth.can("chat"):
        return _forbidden()
    library = _library(state)
    clip = library.get(clip_id) if library is not None else None
    if clip is None:
        return 404, {"error": "motion_not_found"}
    return 200, clip


def list_motion(state: AppState, query: dict, *, auth=None) -> tuple[int, dict]:
    """`GET /api/motion[?tag=]`：除錯用（看庫裡有什麼、每個 tag 有幾段）。"""
    if auth is None:
        return _unauthorized()
    if not auth.can("chat"):
        return _forbidden()
    library = _library(state)
    tags = library.tags() if library is not None else {}
    tag = str((query.get("tag") or [""])[0] or "").strip()
    if tag:
        return 200, {"tag": tag, "ids": list(tags.get(tag, []))}
    return 200, {"count": sum(len(ids) for ids in tags.values()),
                 "tags": {name: list(ids) for name, ids in sorted(tags.items())}}


__all__ = ["get_motion", "list_motion"]
