"""一組心跳（每個角色一條）——從 `runtime/heartbeat.py` 搬出來（本檔已到 600 行上限）。"""

from __future__ import annotations

class HeartbeatSet:
    """一組心跳（每個角色一條）。對外只露 `start/stop/snapshot`。"""

    def __init__(self, beats) -> None:
        self.beats = {beat.entity: beat for beat in beats}

    def __iter__(self):
        return iter(self.beats.values())

    def get(self, entity: str):
        return self.beats.get(str(entity))

    def attach_sleep(self, clocks) -> None:
        """接上生物钟（owner 2026-11-16）：睡着时心跳只做本地推进，不叫心情/独白模型。"""
        from agent.brain.runtime.inner import sleep_gate
        state = sleep_gate.from_clocks(clocks)
        for beat in self.beats.values():
            beat.sleep_state = state

    def start(self) -> None:
        for beat in self.beats.values():
            beat.start()

    def stop(self) -> None:
        for beat in self.beats.values():
            beat.stop()

    def tick_all(self, *, at: float | None = None) -> dict[str, list[dict]]:
        return {name: beat.tick(at=at) for name, beat in self.beats.items()}

    def snapshot(self) -> dict:
        return {"beats": {name: beat.snapshot() for name, beat in self.beats.items()}}


__all__ = ["HeartbeatSet"]
