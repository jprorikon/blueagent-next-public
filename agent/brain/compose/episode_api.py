"""手動封片（owner 調試用；平常由閒置/輪數/字數自動觸發）。"""

from __future__ import annotations

import time

from agent.brain.domain.episode import CutReason
from agent.brain.domain.ids import ThreadKey


def seal(state, *, entity: str = "mira", principal: str = "owner",
         group: bool = False, participants=None, ownerless: bool = False) -> dict:
    scope = "self:trpg-table-1" if ownerless else (
        "group:discord-lobby" if group else principal)
    scene = "trpg-table-1" if ownerless else ("discord-lobby" if group else "together")
    thread = state.app.threads.current_for(
        ThreadKey(scope=scope, entity=entity, scene=scene, purpose="chat"))
    if thread is None or not thread.segment:
        return {"ok": False, "reason": "当前片段还是空的，等她说过几轮再封。",
                "state": state.snapshot(principal=principal, entity=entity,
                                        participants=participants)}
    episode = state.app.archive.seal(thread, time.time(), CutReason.EXPLICIT)
    state.app.threads.persist()
    return {
        "ok": episode is not None,
        "sealed": ({"id": episode.id, "reason": episode.reason.value,
                    "turns": len(episode.turns), "summary": episode.summary}
                   if episode is not None else None),
        "state": state.snapshot(principal=principal, entity=entity,
                                participants=participants),
    }
