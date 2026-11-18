"""把情緒變更送到 App 的常駐 SSE；聊天交付與狀態事件共用傳輸、分開處理。"""

from __future__ import annotations

import threading

from agent.brain.domain.client_end import APP_ENDS


def notify(callback, entity: str, log) -> None:
    """推送是旁路：App 斷線或回調出錯都不能擋住情緒與對話。"""
    if callback is None:
        return
    try:
        callback(str(entity))
    except Exception as exc:                                # noqa: BLE001
        log(f"affect: 情緒推送略過（{type(exc).__name__}: {exc}）")


class MoodEvents:
    """AffectService 的變更回調：只發給主人，手機與 MR 各一份。"""

    def __init__(self, bus, affect, *, principal: str, ends=APP_ENDS) -> None:
        self.bus = bus
        self.affect = affect
        self.principal = str(principal or "owner")
        self.ends = tuple(str(end) for end in ends)
        self._lock = threading.Lock()
        self._last: dict[str, str] = {}

    def publish(self, entity: str) -> int:
        """回實際送達數。idle_s 一直變，不拿它做變更指紋。"""
        entity = str(entity or "")
        mood = self.affect.mood(entity) or {}
        if not entity or not mood:
            return 0
        fingerprint = repr(sorted((key, value) for key, value in mood.items() if key != "idle_s"))
        with self._lock:
            if self._last.get(entity) == fingerprint:
                return 0
            self._last[entity] = fingerprint
        delivered = 0
        for end in self.ends:
            frame = {"kind": "mood", "entity": entity, "end": end, "mood": mood}
            delivered += self.bus.publish(self.principal, frame, end=end)
        return delivered


__all__ = ["MoodEvents"]
