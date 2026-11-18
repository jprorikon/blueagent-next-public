from __future__ import annotations

import pathlib
import sys
import threading
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent.parent   # 仓库根
sys.path.insert(0, str(HERE))

from agent.brain.domain.decision import DispatchMode, Engagement, RouteDecision  # noqa: E402
from agent.brain.domain.entity import CharacterEntity, EntityRegistry       # noqa: E402
from agent.brain.domain.episode import (CutReason, SegmentPolicy, cut_reason,  # noqa: E402
                               seal)
from agent.brain.domain.event import (ConversationMode, IngressEvent, InputKind,  # noqa: E402
                            SourceClass)
from agent.brain.domain.ids import ThreadKey, new_turn_id                    # noqa: E402
from agent.brain.domain.principal import Principal, PrincipalRegistry        # noqa: E402
from agent.brain.domain.thread import (MemoryScope, Thread, ThreadState,      # noqa: E402
                              TurnRecord, legacy_device_bank)
from agent.brain.pipelines.base import (ContextBudget, DEFAULT_CONTEXT_BUDGET, Envelope,  # noqa: E402
                               Pipeline, PipelineRegistry, StageOutcome,
                               TurnContext, VOICE_CONTEXT_BUDGET)
from agent.brain.pipelines.blueprints import BLUEPRINTS, build, default_pipeline  # noqa: E402
from agent.brain.ports import NullPorts                                       # noqa: E402
from agent.brain.router.policy import Router, RouterConfig                    # noqa: E402
from agent.brain.router import rules                                          # noqa: E402
from agent.brain.runtime.voice.arbiter import FloorArbiter                          # noqa: E402
from agent.brain.runtime.dedupe import IdempotencyWindow                      # noqa: E402
from agent.brain.runtime.entity_store import EntityStore                      # noqa: E402
from agent.brain.runtime.episodes import EpisodeArchive                       # noqa: E402
from agent.brain.runtime.ingress import IngressRuntime                        # noqa: E402
from agent.brain.runtime.lane import CancelToken                             # noqa: E402
from agent.brain.runtime.scheduler import Scheduler, SchedulerConfig          # noqa: E402
from agent.brain.runtime.threadstore import ThreadStore                      # noqa: E402
from agent.brain.sources.base import BaseSource, SourceCaps                   # noqa: E402


# ── 共用替身 ──────────────────────────────────────────────────────────────────────
def make_event(*, text: str = "hi", source: str = "phone", at: float = 1000.0,
               cls: SourceClass = SourceClass.INTERACTIVE,
               kind: InputKind = InputKind.TEXT, entity: str = "mira",
               principal: str = "owner", scope: str = "", addressed_to: str = "",
               scene: str = "", purpose: str = "", thread: str = "",
               pipeline: str = "") -> IngressEvent:
    payload: dict = {"text": text}
    if purpose:
        payload["purpose"] = purpose
    return IngressEvent(principal=principal, entity=entity, source=source, kind=kind, at=at,
                        source_class=cls, scope=scope, addressed_to=addressed_to,
                        scene=scene, thread_hint=thread,
                        pipeline_hint=pipeline, payload=payload)


def make_thread(*, entity: str = "mira", scene: str = "together",
                purpose: str = "chat", thread_id: str = "th_test-1",
                now: float = 1000.0) -> Thread:
    thread = Thread(key=ThreadKey("owner", entity, scene, purpose), id=thread_id,
                    lane=SourceClass.INTERACTIVE, created_at=now, last_active_at=now)
    return thread


def make_decision(thread_id: str, *, lane: SourceClass = SourceClass.INTERACTIVE,
                  mode: DispatchMode = DispatchMode.ATTACH,
                  priority: int | None = None) -> RouteDecision:
    key = ThreadKey("owner", "mira", "together", "chat")
    return RouteDecision(
        thread_id=thread_id, key=key, pipeline="chat.text", lane=lane,
        priority=rules.priority_for(lane, rules.purpose_for(make_event())) if priority is None else priority,
        mode=mode, scope=MemoryScope("owner", "mira", "together", thread_id),
        deadline_ms=rules.deadline_for(lane), reason="test", created=False, at=0.0,
    )


class CollectSource(BaseSource):
    def __init__(self, source_id: str, cls: SourceClass = SourceClass.INTERACTIVE, **kw) -> None:
        super().__init__(source_id, cls, **kw)
        self.frames: list = []
        self._lock = threading.Lock()

    def deliver(self, frame) -> None:
        with self._lock:
            self.frames.append(frame)


class SpyStage:
    def __init__(self, name: str, calls: list, *, outcome: StageOutcome | None = None,
                 gate: threading.Event | None = None,
                 entered: threading.Event | None = None,
                 record: str = "", emit: dict | None = None) -> None:
        self.name = name
        self.calls = calls
        self.outcome = outcome
        self.gate = gate
        self.entered = entered
        self.record = record
        self.emit = emit

    def run(self, env, ctx):
        self.calls.append(self.name)
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            self.gate.wait(2.0)
        if self.emit is not None:
            ctx.emit(self.emit.get("kind", "delta"), self.emit.get("payload", {"text": "ok"}))
        if self.record:
            ctx.record("assistant", self.record)
        return self.outcome


def make_pipeline(pipeline_id: str, calls: list, *stages) -> Pipeline:
    return Pipeline(id=pipeline_id, stages=tuple(stages))
