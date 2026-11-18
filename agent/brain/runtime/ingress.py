"""入站執行：**源 → 路由 → 自動切片 → 調度 → 管線 → 源**。

這個檔案就是新架構的主幹，刻意保持很短（不含業務邏輯）：

    1. 解析實體與決策（`Router`）
    2. 更新實體的在場／注意力（`EntityStore`）
    3. 問一句「這條線該封片了嗎」（`EpisodeArchive`）——**沒有使用者入口**
    4. 依決策決定：開始新一輪／排隊／丟棄
    5. 交給 `Scheduler`（它才准起執行緒）
    6. 管線跑完，`Thread.end_turn()` 收尾

舊的 `server.py` 之後只要把每個 HTTP 入口換成「建 Event → `runtime.handle()`」，
`session` 這個參數就可以從對外契約裡消失（保留一輪相容期只為了讓舊客戶端不炸）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable

from agent.brain.domain.decision import DispatchMode, Engagement, RouteDecision
from agent.brain.domain.episode import Episode
from agent.brain.domain.event import FrameKind, IngressEvent, InputKind, OutFrame, SourceClass
from agent.brain.domain.ids import new_turn_id
from agent.brain.domain.thread import Thread, ThreadState, TurnRecord
from agent.brain.pipelines.base import Envelope, PipelineRegistry, TurnContext
from agent.brain.ports import Clock, NullPorts, SystemClock
from agent.brain.router.policy import Router
from agent.brain.runtime.attention import AttentionGate
from agent.brain.runtime.dedupe import IdempotencyWindow
from agent.brain.runtime.episodes import EpisodeArchive
from agent.brain.runtime.entity_store import EntityStore
from agent.brain.runtime.ingress_support import IngressSupport
from agent.brain.runtime.lane import Job
from agent.brain.runtime.scheduler import Scheduler
from agent.brain.runtime.threadstore import ThreadStore
from agent.brain.sources.base import Source


@dataclass(frozen=True, slots=True)
class HandleResult:
    """一次入站的結果。`decision is None`＝這是純在場訊號，沒有回合。"""

    decision: RouteDecision | None
    turn_id: str = ""
    thread: Thread | None = None
    job: Job | None = None
    sealed: Episode | None = None
    dropped: bool = False
    duplicate: bool = False
    observed: bool = False
    reacted: bool = False
    yielded: bool = False
    yield_ms: int = 0

    @property
    def thread_id(self) -> str:
        return self.thread.id if self.thread is not None else ""


def _silent(_: str) -> None:
    return None


def _discard(_) -> None:
    return None


class IngressRuntime(IngressSupport):
    def __init__(self, *, router: Router, threads: ThreadStore, pipelines: PipelineRegistry,
                 scheduler: Scheduler, episodes: EpisodeArchive | None = None,
                 entities: EntityStore | None = None, ports=None,
                 attention: AttentionGate | None = None,
                 dedupe: IdempotencyWindow | None = None,
                 clock: Clock | None = None, log: Callable[[str], None] | None = None,
                 sink: Callable | None = None) -> None:
        self.router = router
        self.threads = threads
        self.pipelines = pipelines
        self.scheduler = scheduler
        self.episodes = episodes
        self.entities = entities
        self.attention = attention
        self.dedupe = dedupe or IdempotencyWindow()
        self.ports = ports or NullPorts()
        self.clock = clock or SystemClock()
        self.log = log or _silent
        self.sink = sink

    # ── 對外 ────────────────────────────────────────────────────────────────────
    def handle(self, event: IngressEvent, *, source: Source | None = None) -> HandleResult:
        at = float(event.at or self.clock.now())

        # 幂等：同一件事只處理一次（桌端重發／斷線重連／webhook at-least-once）。
        if event.idempotency_key:
            token = f"{event.source}:{event.idempotency_key}"
            if self.dedupe.seen(token, at):
                self.log(f"dedupe: 重複事件丟棄 key={token} trace={event.trace_id or '-'}")
                return HandleResult(decision=None, duplicate=True)

        # 純在場訊號（心跳／已讀）：只更新 presence，不建回合、不花 token。
        if event.kind is InputKind.SIGNAL:
            self._note_presence(event, at)
            return HandleResult(decision=None)

        decision = self.router.route(event, now=at)
        thread = self.threads.get(decision.thread_id)
        if thread is None:
            raise RuntimeError(f"routed to a thread that is already gone: {decision.thread_id}")

        self._remember_presence(decision, thread, at)
        replay = bool(event.payload.get("replay"))
        turn_id = new_turn_id()

        if replay:
            decision = replace(decision, engage=Engagement.REPLY,
                               engage_reason="replay", yield_ms=0)
        elif self.attention is not None:
            self.attention.note_human(event, thread, at)
            if event.payload.get("force_reply"):
                decision = replace(decision, engage=Engagement.REPLY,
                                   engage_reason="forced", yield_ms=0)
            else:
                hits = 0
                if decision.engage is Engagement.OBSERVE:
                    probe = getattr(self.ports, "probe", None)
                    if probe is not None:
                        people = set(event.participants or ()) | {event.principal}
                        hits = int(probe(decision.scope, event.text,
                                         participants=people) or 0)
                gate = self.attention.decide(
                    event, thread, decision.engage, recall_hits=hits,
                    reactions=event.reactions, now=at)
                decision = replace(decision, engage=gate.action,
                                   engage_reason=gate.reason, yield_ms=gate.yield_ms)

        if decision.engage is Engagement.IGNORE:
            self.log(decision.to_log() + " ignored=1")
            return HandleResult(decision=decision, thread=thread, dropped=True)

        if decision.engage in {Engagement.SILENT, Engagement.OBSERVE}:
            # owner 2026-10-22：「旁聽不寫記憶，但進 session」——
            #   進：這句留在 thread 的 transcript 裡（短上下文、她自己看得到剛才發生什麼）
            #   不進：不沉成事實（不然群裡每句閒聊都會變長期記憶，越滾越髒）
            self._record_user(event, thread, turn_id, at, decision.engage.value)
            self.log(decision.to_log() + " silent=1（旁聽：進 session、不寫記憶）")
            return HandleResult(decision=decision, thread=thread, observed=True)

        if decision.engage is Engagement.REACT:
            self._record_user(event, thread, turn_id, at, decision.engage.value)
            self._record_memory(decision, event, at)
            self._emit_reaction(event, thread, turn_id, source, decision)
            self.log(decision.to_log() + " reacted=1")
            return HandleResult(decision=decision, thread=thread, reacted=True,
                                observed=True)

        if decision.engage is Engagement.YIELD:
            self._record_user(event, thread, turn_id, at, decision.engage.value)
            self._record_memory(decision, event, at)
            self.log(decision.to_log() + f" yielded={decision.yield_ms}ms")
            return HandleResult(decision=decision, thread=thread, yielded=True,
                                observed=True, yield_ms=decision.yield_ms)

        sealed = self._maybe_seal(thread, decision, at, event.text)

        if decision.mode is DispatchMode.DROP:
            self.log(decision.to_log() + " dropped=1")
            return HandleResult(decision=decision, thread=thread, sealed=sealed, dropped=True)

        deferred = decision.mode is DispatchMode.QUEUE
        if decision.mode is DispatchMode.INTERRUPT:
            with thread.lock:
                thread.interrupt()
            self.scheduler.preempt(thread.id)

        if deferred:
            with thread.lock:
                thread.queued += 1
                thread.state = ThreadState.WAITING
            epoch = thread.epoch
        else:
            epoch = thread.begin_turn(turn_id, at)

        env = Envelope(event=event, decision=decision, thread=thread, turn_id=turn_id, text=event.text)
        pipeline = self.pipelines.require(decision.pipeline)
        sink = self._sink_for(source)

        def run(token) -> None:
            nonlocal epoch
            if deferred:
                with thread.lock:
                    thread.queued = max(0, thread.queued - 1)
                    epoch = thread.begin_turn(turn_id, self.clock.now())
            if not event.payload.get("replay") and not event.payload.get("ack"):
                # `ack`（語音先短回一句）不寫使用者輪、也不進記憶：真正的工作回合會寫一次，
                # 否則同一句話會在 transcript 裡出現兩次（2026-10-22）。
                with thread.lock:
                    thread.record(TurnRecord(turn_id=turn_id, role="user", text=event.text,
                                             source=event.source, at=at, speaker=event.principal,
                                             addressed_to=event.addressed_to, engage="reply"))
            ctx = TurnContext(thread=thread, turn_id=turn_id, epoch=epoch, cancel=token,
                              sink=sink, ports=self.ports, source=event.source,
                              principal=event.principal, log=self.log)
            try:
                pipeline.run(env, ctx)
            finally:
                with thread.lock:
                    thread.end_turn(turn_id)
                if self.attention is not None:
                    self.attention.note_agent(decision, event, self.clock.now())

        def discard() -> None:
            with thread.lock:
                thread.queued = max(0, thread.queued - 1)
                thread.touch(self.clock.now())

        job = self.scheduler.submit(decision, run, on_discard=discard if deferred else None)
        self.log(decision.to_log())
        return HandleResult(decision=decision, turn_id=turn_id, thread=thread,
                            job=job, sealed=sealed)

    def shutdown(self, *, wait: bool = True) -> None:
        self.scheduler.shutdown(wait=wait)

    def snapshot(self) -> dict:
        return {
            "threads": self.threads.snapshot(),
            "entities": self.entities.snapshot() if self.entities else [],
            "episodes": self.episodes.snapshot() if self.episodes else [],
            "scheduler": self.scheduler.stats(),
            "pipelines": self.pipelines.snapshot(),
        }

def build_runtime(*, router: Router, threads: ThreadStore, pipelines: PipelineRegistry,
                  scheduler: Scheduler | None = None, entities: EntityStore | None = None,
                  episodes: EpisodeArchive | None = None, ports=None,
                  dedupe: IdempotencyWindow | None = None,
                  clock: Clock | None = None, log: Callable[[str], None] | None = None,
                  sink: Callable | None = None) -> IngressRuntime:
    """便利建構子：沒給的就用安全的預設（不接線也能跑通形狀）。"""
    return IngressRuntime(
        router=router, threads=threads, pipelines=pipelines,
        scheduler=scheduler or Scheduler(), entities=entities,
        episodes=episodes, ports=ports or NullPorts(), dedupe=dedupe,
        clock=clock or SystemClock(), log=log, sink=sink,
    )
