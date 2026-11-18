"""狀態與營運：/api/health、/api/state、/api/mood、/api/metrics、/api/reset。"""

from __future__ import annotations

import os
from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext, Role

from agent.server.api.common import _owner_required, _unauthorized


def health(state: AppState, auth: AuthContext | None = None) -> tuple[int, dict]:
    accounts = getattr(getattr(state, "app", None), "accounts", None)
    body = {"ok": True, "engine": "blueagent-next",
            # App 契約版本（Unity 端照 `docs/APP-API-SSE.md` 實作）
            "api": "sse-v1",
            # 帳號檔開機時讀不到 ⇒ 唯讀（App 建的 guest 這段期間可能 401）；只給布林，不給路徑／錯誤
            "accounts_readonly": bool(getattr(accounts, "readonly", False))}
    # 探活可以公開，記憶庫與工作負載的規模不行；狀態頁帶 owner token 才附完整 counts。
    if auth is not None and auth.is_owner:
        body["counts"] = state.snapshot()["counts"]
    return 200, body


def get_mood(state: AppState, query: dict,
             auth: AuthContext | None = None) -> tuple[int, dict]:
    """`GET /api/mood?entity=&limit=`：她現在什麼心情、為什麼（owner 2026-10-24）。

    回四塊：
      · `mood`    ：valence／arousal／label／propensity（跟 `/api/state` 同一個來源）
      · `block`   ：真的貼進 prompt 的那段語氣指示（給你看她「被要求怎麼說話」）
      · `timeline`：最近的評分流水（時間／種類／valence 增量／arousal 增量）
      · `stats`   ：近 24 小時統計（正負比例、平均增量）——判斷「是不是只會被推高」
    """
    if auth is None or not getattr(auth, "is_owner", False):
        # ⚠️ 2026-11-02：從前這裡回 `_unauthorized()`（401）——契約（docs/accounts-and-clients.md §4）說
        # 得很清楚：**401＝token 壞了／被撤銷，端末要回登入頁；403＝token 有效但這個帳號不能做**。
        # 回 401 會讓 guest 端把「看不到她的心情」誤判成「登入失效」而被踢回登入頁。改用 `_owner_required`。
        return _owner_required(auth) or _unauthorized()
    entity = str((query.get("entity") or ["mira"])[0] or "mira").strip()
    try:
        limit = max(1, min(200, int((query.get("limit") or ["20"])[0])))
    except (TypeError, ValueError):
        limit = 20
    affect = getattr(getattr(state, "app", None), "affect", None)
    if affect is None:
        return 200, {"ok": False, "reason": "affect_disabled", "entity": entity}
    mood = affect.mood(entity) or {}
    view = str((query.get("view") or [""])[0] or "").strip()
    if view == "badge":
        # App 徽章只需要權威快照；timeline／stats 留給完整面板，避免兩分鐘一次白傳。
        return 200, {"ok": True, "entity": entity, "mood": mood}
    return 200, {
        "ok": True, "entity": entity, "mood": mood,
        "block": state.inner.mood_block(mood),
        "timeline": affect.timeline(entity, limit=limit),
        "stats": affect.stats(entity),
    }


def get_state(state: AppState, query: dict,
              auth: AuthContext | None = None) -> tuple[int, dict]:
    if auth is None:
        return _unauthorized()
    principal = auth.principal
    room = None
    if auth.is_owner:
        principal = (query.get("principal") or [auth.principal])[0]
        room_raw = (query.get("room") or [""])[0]
        room = [part for part in room_raw.split(",") if part.strip()] or None
    entity = (query.get("entity") or ["mira"])[0]
    try:
        body = state.snapshot(principal=principal, entity=entity,
                              participants=room, admin=auth.is_owner)
        view = str((query.get("view") or [""])[0] or "").strip()
        if view == "resume":
            # App 回前景只對帳目前角色；不把記憶、心跳、預算與其他角色的線一起傳。
            rows = [{"id": row.get("id"), "entity": row.get("entity"),
                     "last_active_at": row.get("last_active_at")}
                    for row in body.get("threads", []) if row.get("entity") == entity]
            return 200, {"ok": True, "entity": entity, "threads": rows}
        # 情緒與心跳（主人的面板要看得見「她現在什麼心情」）
        affect = getattr(state.app, "affect", None)
        if affect is not None and auth.is_owner:
            body["affect"] = affect.mood(entity)
            beat = getattr(state.app, "heartbeat", None)
            if beat is not None:
                snapshot = beat.snapshot()
                # 每個角色自己的心跳（`beats`）＋「我這個 entity 的那條」放頂層，方便前端取
                body["heartbeat"] = snapshot
                body["heartbeat_self"] = (snapshot.get("beats") or {}).get(entity, {})
            pocket = getattr(state.app, "budget", None)
            if pocket is not None and hasattr(pocket, "mood"):
                body["budget"] = pocket.mood(entity)
            router = getattr(state.app, "delivery", None)
            if router is not None and hasattr(router, "snapshot"):
                body["delivery"] = router.snapshot()
        return 200, body
    except KeyError as exc:
        return 400, {"error": f"unknown id: {exc}"}


def post_metrics(state: AppState, auth: AuthContext | None = None) -> tuple[int, dict]:
    """這一輪的統計：p50/p95 延遲、語音失敗數、各角色輪數。只有主人能看。"""
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    recorder = getattr(state.app, "telemetry", None)
    if recorder is None:
        return 503, {"error": "telemetry_unavailable"}
    metrics = recorder.metrics()
    # 資料治理：輪轉次數（日誌／封存）＋被封存的 turn 數
    threads = getattr(state.app, "threads", None)
    activity = getattr(state.app, "activity_log", None)
    metrics["archive"] = {
        "thread_turns": int(getattr(threads, "archived", 0) or 0),
        "activity_rotations": int(getattr(activity, "rotations", 0) or 0),
    }
    # 主動消息：閘門統計＋每個角色心跳（sent/skipped/last_skip）——「她為什麼沒開口」要看得見
    proactive = getattr(state.app, "proactive", None)
    if proactive is not None and hasattr(proactive, "snapshot"):
        try:
            metrics["proactive"] = proactive.snapshot()
        except Exception:                                # noqa: BLE001
            pass
    beats = getattr(state.app, "heartbeats", None) or getattr(state.app, "heartbeat", None)
    if beats is not None and hasattr(beats, "__iter__"):
        try:
            metrics["heartbeats"] = [beat.snapshot() for beat in beats]
        except Exception:                                # noqa: BLE001
            pass
    # SPIN 狀態引出（P1）：引出幾次、失敗幾次、平均毫秒
    for pipeline_id in ("chat.text", "voice.realtime", "trpg.turn"):
        pipeline = getattr(state.app, "pipelines", None)
        stages = []
        try:
            found = pipeline.get(pipeline_id) if pipeline is not None else None
            stages = list(getattr(found, "stages", ()) or ())
        except Exception:                                 # noqa: BLE001
            stages = []
        stage = next((row for row in stages if getattr(row, "name", "") == "state.elicit"), None)
        if stage is not None and hasattr(stage, "snapshot"):
            metrics["state_elicit"] = stage.snapshot()
            break

    # 分級輪轉（按天切＋保留期）：每個檔幾個日檔、最舊／最新、保留幾天
    daily: dict[str, dict] = {}
    for name, owner in (("turns", recorder),
                        ("attention", getattr(state.app, "attention_history", None)),
                        ("episodes", getattr(state.app, "episodes", None))):
        guard = getattr(owner, "daily", None) or getattr(owner, "_daily", None)
        if guard is not None and hasattr(guard, "summary"):
            daily[name] = guard.summary()
    activity_daily = getattr(activity, "_daily", None)
    if isinstance(activity_daily, dict) and activity_daily:
        first = next(iter(activity_daily.values()))
        daily["activity"] = first.summary()
    if daily:
        metrics["daily_rotation"] = daily
    # 告警狀態（冷卻中／已送幾次）＋ 指令佇列（排到哪）
    alerts = getattr(state.app, "alerts", None)
    if alerts is not None:
        metrics["alerts"] = alerts.snapshot()
    queue = getattr(state, "commands", None)
    if queue is not None:
        snapshot = queue.snapshot(limit=5)
        metrics["commands"] = {"pending": snapshot["pending"],
                               "stats": snapshot["stats"]}
    # 固定段指紋：改了人格檔／user.md 的 identity，這裡的 hash 會變
    prompt_stats = getattr(state.app, "prompt_stats", None)
    if prompt_stats:
        metrics["prompt"] = dict(prompt_stats)
    # TRPG 席位：叫醒幾次、出手幾次、被拒幾次（她那一側的帳）
    seat = getattr(state.app, "trpg", None)
    if seat is not None:
        metrics["trpg"] = seat.health()
    return 200, {"ok": True, "metrics": metrics,
                 "recent": recorder.tail(10)}


def post_reset(state: AppState, auth: AuthContext | None = None) -> tuple[int, dict]:
    """整桌重置：**默認關**（`BLUEAGENT_ALLOW_RESET=1` 才開）。

    為什麼要有這道：這是唯一會把主人累積的記憶/執行緒一起清掉的端點。容器裡一個誤點、
    或哪天把服務掛到反代上忘了收，代價是全部歸零。默認關、要開必須顯式。
    """
    denied = _owner_required(auth)
    if denied is not None:
        return denied
    if str(os.environ.get("BLUEAGENT_ALLOW_RESET") or "").strip().lower() not in {
            "1", "on", "true", "yes"}:
        return 403, {"error": "reset_disabled",
                     "hint": "要重置請設 BLUEAGENT_ALLOW_RESET=1"}
    return 200, {"ok": True, "state": state.reset()}
