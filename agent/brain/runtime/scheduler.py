"""調度器：**實時源並行、非實時源排隊**（使用者 2026-10-21 拍板）。

一張表就能看完所有併發語義：

| 源分類      | 併發            | 槽位 | 排隊 | 可搶佔 | 同一條線串行 |
|-------------|-----------------|------|------|--------|--------------|
| REALTIME    | 並行            | 8    | 否   | 是     | 否（舊的被取消） |
| INTERACTIVE | 排隊（受槽位）  | 2    | 是   | 否     | 是           |
| AMBIENT     | 排隊（最後）    | 1    | 是   | 否     | 是           |

關鍵點：
  · 排隊不是「一條全域 FIFO」。每個線有自己的郵箱，調度器按 `(priority, seq)` 挑，
    所以一條慢的環境線不會把所有人的打字都堵住。
  · 同一個 thread 的非實時回合絕不並行（`_busy_threads`），這是舊模型缺的保證
    （2026-10-20 評審 §1-4 的 kick 重複執行就是這個缺口）。
  · 搶佔是**協作式**的：只取消令牌，不強殺執行緒；輸出由 `TurnContext.emit` 的 epoch 守衛兜底。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from agent.brain.domain.decision import DispatchMode, RouteDecision
from agent.brain.domain.event import SourceClass
from agent.brain.runtime.lane import CancelToken, Job, Lane


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    realtime_slots: int = 8
    interactive_slots: int = 2
    ambient_slots: int = 1
    #: 排隊上限（**每個角色**算；owner 2026-10-22：「上線 10」）。
    #: 滿了就丢「最不重要且最舊」的那一條——不是丢最新的，也不是丢語音那條。
    queue_max: int = 10
    #: 排隊多久算作廢（owner 2026-10-22：「作廢事件延長」）⇒ 從「一有新的就頂掉」放寬到 3 分鐘。
    #: 語音源不排隊（REALTIME 直接並行），所以這個值只影響打字/環境源。
    stale_ms: int = 180_000


class Scheduler:
    def __init__(self, config: SchedulerConfig | None = None, *, name: str = "ba-lanes",
                 entities: int = 1) -> None:
        self._cfg = config or SchedulerConfig()
        #: **每個角色**的槽位（owner 2026-10-21：mira 做 A 的時候 aster 要做 B）。
        #: 所以槽位是 per-entity 預算，不是全域配額；同一條線仍嚴格串行。
        self._slots: dict[SourceClass, int] = {
            SourceClass.REALTIME: max(1, self._cfg.realtime_slots),
            SourceClass.INTERACTIVE: max(1, self._cfg.interactive_slots),
            SourceClass.AMBIENT: max(1, self._cfg.ambient_slots),
        }
        self._active: dict[tuple[str, SourceClass], int] = {}
        self._pending: list[Job] = []
        self._lanes: dict[str, Lane] = {}
        self._busy_threads: set[str] = set()      # 只給非實時：保證同線串行
        self._cond = threading.Condition()
        self._seq = 0
        self._stopped = False
        self.dropped = 0          # 因佇列滿被丢掉的
        self.expired = 0          # 排隊超時作廢的
        self._name = name
        self._entities = max(1, int(entities))
        self._executor = ThreadPoolExecutor(
            max_workers=sum(self._slots.values()) * self._entities + 1,
            thread_name_prefix=name,
        )
        self._dispatcher = threading.Thread(target=self._dispatch, name=f"{name}-dispatch", daemon=True)
        self._dispatcher.start()

    # ── 對外 ────────────────────────────────────────────────────────────────────
    def submit(self, decision: RouteDecision, fn: Callable[[CancelToken], None], *,
               on_discard: Callable[[], None] | None = None) -> Job:
        with self._cond:
            if self._stopped:
                raise RuntimeError("scheduler is stopped")
            self._seq += 1
            job = Job(thread_id=decision.thread_id, lane=decision.lane,
                      priority=decision.priority, fn=fn, seq=self._seq,
                      entity=str(getattr(getattr(decision, "key", None), "entity", "") or ""),
                      queued_at=time.monotonic(), on_discard=on_discard)
            lane = self._lanes.get(decision.thread_id)
            if lane is None:
                lane = Lane(thread_id=decision.thread_id, priority=decision.priority)
                self._lanes[decision.thread_id] = lane
            lane.priority = min(lane.priority, decision.priority)
            lane.pending += 1
            if decision.mode is DispatchMode.INTERRUPT:
                lane.preempt()
            self._pending.append(job)
            self._enforce_capacity_locked(str(job.entity))
            self._cond.notify_all()
            return job

    def _enforce_capacity_locked(self, entity: str) -> None:
        """每個角色的排隊上限；滿了就丢「最不重要 ＋ 最舊」那條（並讓它知道自己被作廢）。

        為什麼丢舊的：排隊的意義是「等一下還能用」。丢最新的等於把剛說的話忘掉；
        丢最舊的才是「這句已經過時了」。優先級高的（數字小的，例如語音）先保住。
        """
        limit = max(1, int(self._cfg.queue_max))
        mine = [job for job in self._pending if str(job.entity) == entity]
        if len(mine) <= limit:
            return
        # 先丢優先級最差（數字最大）的；同級別裡丢最舊的
        mine.sort(key=lambda job: (-job.priority, job.seq))
        for job in mine[:len(mine) - limit]:
            self._pending.remove(job)
            lane = self._lanes.get(job.thread_id)
            if lane is not None:
                lane.pending = max(0, lane.pending - 1)
            job.discard("queue_full")
            self.dropped += 1

    def _expire_locked(self, now: float) -> None:
        """排隊超過 `stale_ms` ⇒ 作廢（不再執行，也不再等）。"""
        horizon = max(0.0, float(self._cfg.stale_ms) / 1000.0)
        if horizon <= 0:
            return
        for job in list(self._pending):
            if now - float(getattr(job, "queued_at", now)) < horizon:
                continue
            self._pending.remove(job)
            lane = self._lanes.get(job.thread_id)
            if lane is not None:
                lane.pending = max(0, lane.pending - 1)
            job.discard("queue_expired")
            self.expired += 1

    def preempt(self, thread_id: str) -> bool:
        with self._cond:
            lane = self._lanes.get(str(thread_id))
            return lane.preempt() if lane is not None else False

    def drain(self, timeout: float = 5.0) -> bool:
        """等所有已提交的回合跑完（測試／關機用）。"""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._pending or any(self._active.values()):  # noqa: SIM108 - 兩條件都要看
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def stats(self) -> dict:
        with self._cond:
            active_total = {cls: 0 for cls in SourceClass}
            active_by_entity: dict[str, dict[str, int]] = {}
            for (entity, lane), count in self._active.items():
                active_total[lane] += count
                if count:
                    active_by_entity.setdefault(entity or "-", {})[lane.label] = count
            return {
                "pending": len(self._pending),
                "queue_max": int(self._cfg.queue_max),
                "dropped_full": self.dropped,
                "expired": self.expired,
                "active": {cls.label: active_total[cls] for cls in SourceClass},
                "active_by_entity": active_by_entity,
                "slots": {cls.label: self._slots[cls] for cls in SourceClass},
                "slots_per_entity": True,
                "lanes": {tid: {"pending": lane.pending, "running": lane.running}
                          for tid, lane in self._lanes.items()},
                "busy_threads": sorted(self._busy_threads),
            }

    def shutdown(self, *, wait: bool = True) -> None:
        with self._cond:
            self._stopped = True
            self._pending.clear()
            self._cond.notify_all()
        self._executor.shutdown(wait=wait, cancel_futures=True)

    # ── 內部 ────────────────────────────────────────────────────────────────────
    def _dispatch(self) -> None:
        while True:
            with self._cond:
                while not self._stopped:
                    job = self._pick_locked()
                    if job is not None:
                        break
                    # 帶 timeout 的等待：沒有新工作時也要醒來清「排隊超時作廢」的
                    self._cond.wait(0.5)
                if self._stopped:
                    return
                assert job is not None
                self._pending.remove(job)
                key = (job.entity, job.lane)
                self._active[key] = self._active.get(key, 0) + 1
                if job.lane is not SourceClass.REALTIME:
                    self._busy_threads.add(job.thread_id)
                try:
                    self._executor.submit(self._run, job)
                except RuntimeError:              # executor 已關
                    self._undo_locked(job)
                    return

    def _pick_locked(self) -> Job | None:
        self._expire_locked(time.monotonic())
        best: Job | None = None
        for job in self._pending:
            # 槽位是「這個角色 × 這一類源」的預算：別的角色的回合不佔我的位
            if self._active.get((job.entity, job.lane), 0) >= self._slots[job.lane]:
                continue
            if job.lane is not SourceClass.REALTIME and job.thread_id in self._busy_threads:
                continue
            if best is None or (job.priority, job.seq) < (best.priority, best.seq):
                best = job
        return best

    def _undo_locked(self, job: Job) -> None:
        key = (job.entity, job.lane)
        self._active[key] = max(0, self._active.get(key, 0) - 1)
        if job.lane is not SourceClass.REALTIME:
            self._busy_threads.discard(job.thread_id)
        lane = self._lanes.get(job.thread_id)
        if lane is not None:
            lane.pending = max(0, lane.pending - 1)
        job.done.set()
        self._cond.notify_all()

    def _run(self, job: Job) -> None:
        lane = self._lanes.get(job.thread_id)
        with self._cond:
            if lane is not None:
                lane.current = job.token
                lane.running += 1
        try:
            job.fn(job.token)
        except BaseException as exc:              # noqa: BLE001 - 一輪炸掉不該殺掉調度器
            job.error = exc
        finally:
            with self._cond:
                key = (job.entity, job.lane)
                self._active[key] = max(0, self._active.get(key, 0) - 1)
                if job.lane is not SourceClass.REALTIME:
                    self._busy_threads.discard(job.thread_id)
                lane = self._lanes.get(job.thread_id)
                if lane is not None:
                    lane.running = max(0, lane.running - 1)
                    lane.pending = max(0, lane.pending - 1)
                    if lane.current is job.token:
                        lane.current = None
                    if lane.running == 0 and lane.pending == 0:
                        self._lanes.pop(job.thread_id, None)
                job.done.set()
                self._cond.notify_all()
