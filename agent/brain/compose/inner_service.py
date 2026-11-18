"""情緒與自主任務的應用服務。"""

from __future__ import annotations

from agent.brain.runtime.inner.affect import mood_block
from agent.brain.runtime.self_tasks import decide


class InnerService:
    def __init__(self, app) -> None:
        self._app = app

    @staticmethod
    def mood_block(mood: dict) -> str:
        return mood_block(mood)

    def self_task(self, entity: str) -> dict:
        affect = getattr(self._app, "affect", None)
        mood = {}
        idle = 0.0
        if affect is not None:
            try:
                mood = affect.mood(entity) or {}
                idle = float(affect.idle_s(entity) or 0.0)
            except Exception:  # noqa: BLE001 - 心情不可用時仍回預設決策
                mood = {}
        decision = decide(valence=float(mood.get("valence") or 0.0),
                          arousal=float(mood.get("arousal") or 0.0),
                          idle_s=idle, quiet_hours=False, busy=False)
        return {"ok": True, "entity": entity,
                "mood": {"valence": mood.get("valence"), "arousal": mood.get("arousal")},
                "idle_s": round(idle, 1), "decision": decision.as_dict()}
