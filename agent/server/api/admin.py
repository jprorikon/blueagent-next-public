"""腦端管理頁的資料（owner 2026-11-09「把脑端的管理页面做出来」）：`/admin`。

三件事：
  ① 思考日誌：她腦子裡在想什麼——獨白（`data/inner/<她>.jsonl`）、提前想（`data/prethink/`）、
     心情定位（`data/mood/`）、私人日誌（「気が乗らない」「前と同じことを…」這類），加上心跳此刻的判斷
     （為什麼沒開口、出口閘的結論）。只讀。
  ② 任務與看板：沿用 `GET /api/tasks`（board／calendar）與 `POST /api/tasks/comment`，這裡不重做。
  ③ 帳號發行與外部帳號綁定：發行／輪替／撤銷沿用 `/api/accounts*`；這裡加「列出每個帳號綁了哪些外部帳號」
     與綁定／解除（核心帳號在腦端，`brain/app/identity.py`）。

全部只有主人能用（`_owner_required`）。
"""

from __future__ import annotations

import json
import pathlib
import time

from agent.brain.compose.state import AppState

from agent.server.api.common import _owner_required

#: 管理頁可以綁的外部來源
SOURCES = ("app", "discord")


def _entity(state: AppState, query: dict) -> str:
    entity = str((query.get("entity") or ["mira"])[0] or "mira").strip()
    entities = getattr(state.app, "entities", None)
    return entity if entities is None or entities.get(entity) is not None else ""


def _limit(query: dict, default: int = 60) -> int:
    try:
        return max(1, min(300, int((query.get("limit") or [default])[0])))
    except (TypeError, ValueError):
        return default


def _jsonl_tail(path: pathlib.Path, limit: int) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _json(path: pathlib.Path) -> dict:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return row if isinstance(row, dict) else {}


def thoughts(state: AppState, entity: str, limit: int = 60) -> dict:
    """她此刻的狀態＋最近的思考（新的在前）。"""
    base = pathlib.Path(getattr(state.app, "data_dir", None) or "data")
    rows: list[dict] = []
    for row in _jsonl_tail(base / "inner" / f"{entity}.jsonl", limit):
        rows.append({"at": row.get("at"), "kind": "monologue", "text": row.get("text"),
                     "action": row.get("action"), "why": row.get("why")})
    pre = _json(base / "prethink" / f"{entity}.json")
    if pre.get("note"):
        rows.append({"at": pre.get("at"), "kind": "prethink", "text": pre.get("note"),
                     "used": bool(pre.get("used_at") or pre.get("used"))})
    mood = _json(base / "mood" / f"{entity}.json")
    if mood.get("read"):
        rows.append({"at": mood.get("at"), "kind": "mood", "text": mood.get("read"),
                     "label": mood.get("label")})
    log = getattr(state.app, "activity_log", None)
    if log is not None:
        for row in log.entries(entity, limit=limit, visibility="private"):
            rows.append({"at": row.get("at"), "kind": "private", "text": row.get("text"),
                         "label": row.get("kind")})
    rows.sort(key=lambda row: float(row.get("at") or 0.0), reverse=True)
    now: dict = {}
    beats = getattr(state.app, "heartbeat", None)
    beat = beats.get(entity) if beats is not None and hasattr(beats, "get") else None
    if beat is not None:
        snap = beat.snapshot()
        now = {key: snap.get(key) for key in ("ticks", "spoken", "last_skip", "last_talk_route",
                                              "last_premise", "interval_s", "threshold")}
        now["mood"] = {key: (snap.get("mood") or {}).get(key)
                       for key in ("label", "valence", "arousal", "propensity", "social_need")}
    affect = getattr(state.app, "affect", None)
    if not (now.get("mood") or {}).get("label") and affect is not None:
        try:                                               # 心跳還沒跳過 ⇒ 直接讀她此刻的心情
            mood = affect.mood(entity) or {}
            now["mood"] = {key: mood.get(key) for key in ("label", "valence", "arousal",
                                                          "propensity", "social_need")}
        except Exception:                                  # noqa: BLE001
            pass
    proactive = getattr(state.app, "proactive", None)
    if proactive is not None and hasattr(proactive, "unanswered"):
        try:
            now["unanswered"] = len(proactive.unanswered(entity))
            now["delivery_state"] = proactive.delivery_state(entity)
        except Exception:                                  # noqa: BLE001 - 管理頁不該因閘出錯而掛
            pass
    lives = getattr(state.app, "lives", None)
    loop = (lives or {}).get(entity) if isinstance(lives, dict) else None
    if loop is not None:
        activity = (loop.snapshot() or {}).get("activity") or {}
        now["activity"] = {key: activity.get(key) for key in ("title", "status", "cursor", "total")}
    return {"entity": entity, "now": now, "thoughts": rows[:limit]}


def get_thoughts(state: AppState, query: dict, *, auth=None) -> tuple[int, dict]:
    """`GET /api/admin/thoughts?entity=&limit=`"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity = _entity(state, query)
    if not entity:
        return 404, {"error": "unknown_entity"}
    return 200, thoughts(state, entity, _limit(query))


def aem(state: AppState, entity: str, *, hours: float = 24.0, step: float = 300.0,
        events: int = 60, now: float | None = None) -> dict:
    """AEM 可視化（owner 2026-11-09「AEM 相關數值也做成可視化」）：此刻／曲線／事件／原因／出口。

    曲線不是取樣記錄，是**關閉式投影**：valence／arousal 由 episode 疊加衰減而成，
    所以任意歷史時刻都能重算（`AffectService.series`）——不新增任何存儲。
    """
    affect = getattr(state.app, "affect", None)
    moment = float(now if now is not None else time.time())
    body: dict = {"entity": entity, "hours": hours, "step": step, "generated_at": moment,
                  "available": affect is not None}
    if affect is None:
        return body
    try:
        mood = affect.mood(entity, at=moment) or {}
    except Exception:                                      # noqa: BLE001 - 管理頁不因情緒層出錯而掛
        mood = {}
    try:
        idle = round(float(affect.idle_s(entity, at=moment)), 1)
    except Exception:                                      # noqa: BLE001
        idle = None
    body["now"] = {
        "label": mood.get("label"), "valence": mood.get("valence"), "arousal": mood.get("arousal"),
        "social_need": mood.get("social_need"), "propensity": mood.get("propensity"),
        "idle_s": idle,
    }
    try:
        body["series"] = affect.series(entity, hours=hours, step=step, at=moment)
    except Exception:                                      # noqa: BLE001
        body["series"] = []
    since = moment - float(hours) * 3600.0
    try:
        rows = affect.annotated_timeline(entity, limit=events, since=since)
    except Exception:                                      # noqa: BLE001
        rows = []
    body["events"] = rows
    try:
        body["cause"] = affect.cause(entity, at=moment)
    except Exception:                                      # noqa: BLE001
        body["cause"] = {}
    beat = None
    beats = getattr(state.app, "heartbeat", None)
    beat = beats.get(entity) if beats is not None and hasattr(beats, "get") else None
    if beat is not None:
        snap = beat.snapshot()
        body["heartbeat"] = {key: snap.get(key) for key in
                             ("ticks", "spoken", "skipped", "last_skip", "talk_route",
                              "last_talk_route", "last_premise", "interval_s", "threshold")}
    proactive = getattr(state.app, "proactive", None)
    if proactive is not None and hasattr(proactive, "unanswered"):
        try:
            body["proactive"] = {"unanswered": len(proactive.unanswered(entity)),
                                 "delivery_state": proactive.delivery_state(entity)}
        except Exception:                                  # noqa: BLE001
            pass
    budget = getattr(state.app, "budget", None)
    if budget is not None and hasattr(budget, "mood"):
        try:
            body["budget"] = budget.mood(entity)
        except Exception:                                  # noqa: BLE001
            pass
    return body


def get_aem(state: AppState, query: dict, *, auth=None) -> tuple[int, dict]:
    """`GET /api/admin/aem?entity=&hours=24&step=300&events=60`（只有主人）。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    entity = _entity(state, query)
    if not entity:
        return 404, {"error": "unknown_entity"}

    def number(name: str, default: float, low: float, high: float) -> float:
        try:
            value = float((query.get(name) or [default])[0])
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    return 200, aem(state, entity, hours=number("hours", 24.0, 0.5, 336.0),
                    step=number("step", 300.0, 60.0, 3600.0),
                    events=int(number("events", 60.0, 1.0, 300.0)))


def get_identities(state: AppState, *, auth=None) -> tuple[int, dict]:
    """`GET /api/admin/identities`：每個腦端帳號 ＋ 綁了哪些外部帳號 ＋ 目前生效的綁定表。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    accounts = getattr(state.app, "accounts", None)
    principals = getattr(state.app, "principals", None)
    rows = []
    for row in (accounts.list(include_revoked=True) if accounts is not None else []):
        rows.append({key: row.get(key) for key in ("account_id", "role", "principal", "display",
                                                   "capabilities", "created_at", "last_seen_at",
                                                   "revoked_at")}
                    | {"links": accounts.links(str(row.get("account_id") or ""))})
    bindings = principals.bindings() if principals is not None and hasattr(principals, "bindings") else []
    return 200, {"accounts": rows, "bindings": bindings, "sources": list(SOURCES)}


def _link_args(body: dict) -> tuple[str, str, str]:
    return (str(body.get("account_id") or "").strip(), str(body.get("source") or "").strip().lower(),
            str(body.get("external_id") or "").strip())


def post_link(state: AppState, body: dict, *, auth=None, unlink: bool = False) -> tuple[int, dict]:
    """`POST /api/admin/identities/link|unlink {account_id, source, external_id}`：寫進帳號表，並即時生效。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    account_id, source, external_id = _link_args(body)
    if not account_id or source not in SOURCES or not external_id:
        return 400, {"error": "bad_request", "sources": list(SOURCES)}
    accounts = getattr(state.app, "accounts", None)
    principals = getattr(state.app, "principals", None)
    if accounts is None or principals is None:
        return 503, {"error": "accounts_unavailable"}
    row = accounts.get(account_id)
    if row is None:
        return 404, {"error": "unknown_account"}
    taken = principals.lookup(source, external_id)
    if not unlink and taken and taken != _principal_of(state, row):
        return 409, {"error": "already_linked", "principal": taken}
    try:
        if unlink:
            accounts.unlink(account_id, source, external_id)
            principals.unbind(source, external_id)
        else:
            accounts.link(account_id, source, external_id)
    except (KeyError, ValueError) as exc:
        return 400, {"error": str(exc)}
    from agent.brain.compose.identity import bind_identities
    bind_identities(state.app)                             # 重建綁定表（新綁的立刻生效）
    auth_store = getattr(state.app, "auth", None)
    if auth_store is not None and hasattr(auth_store, "audit"):
        auth_store.audit("identity.unlink" if unlink else "identity.link", auth=auth,
                         path="/api/admin/identities")
    return get_identities(state, auth=auth)


def _principal_of(state: AppState, row: dict) -> str:
    principals = getattr(state.app, "principals", None)
    if str(row.get("role") or "") == "master" and principals is not None:
        return principals.default()
    return str(row.get("principal") or "")


__all__ = ["aem", "get_aem", "get_identities", "get_thoughts", "post_link", "thoughts"]
