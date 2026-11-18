"""把運行時狀態投影成 UI 快照；不改變任何狀態。"""

from __future__ import annotations

import time


def _health(app, name: str):
    """子系統健康：有 `health()` 才問（測試替身可能沒有），問壞了不該讓整個 snapshot 掛。"""
    target = getattr(app, name, None)
    probe = getattr(target, "health", None)
    if not callable(probe):
        return None
    try:
        return probe()
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _default_life(app) -> dict:
    """`life` 保留成「預設角色那一條」，讓舊前端／舊測試不用改；多角色的看 `lives`。"""
    default = app.entities.default()
    target = default.id if default is not None else "mira"
    loop = app.lives.get(target)
    return loop.snapshot() if loop is not None else {}


def build_snapshot(app, *, principal: str, entity: str, participants,
                   route_log: list, started_at: float) -> dict:
    room = tuple(participants) if participants is not None else (principal,)
    threads = []
    for thread in app.threads.threads():
        threads.append({
            "id": thread.id, "scope": thread.key.scope, "entity": thread.key.entity,
            "scene": thread.key.scene, "purpose": thread.key.purpose,
            "state": thread.state.value,
            "ownerless": thread.key.scope.startswith("self"),
            "participants": sorted(thread.participants),
            "sources": sorted(thread.sources), "epoch": thread.epoch,
            "turns": thread.turns, "episode": thread.episode_index,
            "inflight": thread.inflight, "queued": thread.queued,
            "segment": len(thread.segment), "carry": thread.carry_summary,
            "room": sorted(thread.participants),
            "last_active_at": round(thread.last_active_at, 3),
            "messages": [
                {"role": turn.role, "text": turn.text, "speaker": turn.speaker,
                 "addressed_to": turn.addressed_to, "engage": turn.engage,
                 "at": round(turn.at, 3), "turn": turn.turn_id}
                for turn in list(thread.transcript)[-60:]
            ],
        })
    memory = app.memory.snapshot(room, entity=entity)
    names = {principal.id: principal.name for principal in app.principals.all()}
    stance = app.stances.describe(entity, principal, names=names) if principal else None
    stances = app.stances.snapshot(entity, names=names) if principal == "owner" else []
    episodes = []
    for episode in app.archive.all():
        episodes.append({
            "id": episode.id, "entity": episode.entity, "index": episode.index,
            "reason": episode.reason.value, "turns": len(episode.turns),
            "chars": episode.chars, "summary": episode.summary,
            "started_at": episode.started_at, "ended_at": episode.ended_at,
            "preview": [f"{turn.speaker or turn.role}: {turn.text[:24]}"
                        for turn in episode.turns[:4]],
        })
    return {
        "now": round(time.time(), 3),
        "uptime": round(time.time() - started_at, 1),
        "view": {"principal": principal, "entity": entity},
        "entities": app.entities.snapshot(),
        "principals": app.principals.snapshot(),
        "threads": threads,
        "presence": app.entity_store.snapshot(),
        "episodes": episodes[-10:],
        "memory": memory,
        "userfile": (app.userfile.snapshot() if getattr(app, "userfile", None) else None),
        "discord": (_health(app, "discord")),
        "stt": (_health(app, "stt")),
        "social": app.social.snapshot(),
        "stance": stance,
        "stances": stances,
        "voice": app.voice.snapshot(),
        "life": _default_life(app),
        "lives": {key: loop.snapshot() for key, loop in app.lives.items()},
        "room": sorted(room),
        "route_log": route_log,
        "scheduler": app.scheduler.stats(),
        "pipelines": app.pipelines.snapshot(),
        "counts": {
            "threads": len(threads),
            "episodes": len(episodes),
            "visible_facts": sum(memory["visible_counts"].values()),
            "hidden_facts": sum(memory["hidden_counts"].values()),
            "visible_layers": memory["visible_counts"],
            "hidden_layers": memory["hidden_counts"],
        },
    }
