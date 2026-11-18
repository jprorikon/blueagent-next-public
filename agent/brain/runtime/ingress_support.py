"""IngressRuntime 的內部雜務：記錄、反應幀、在場、切片。

拆出來只是為了讓主幹（ingress.py）保持短；這些方法都靠 self 的依賴。
"""

from __future__ import annotations

from typing import Callable

from agent.brain.domain.decision import Engagement, RouteDecision
from agent.brain.domain.episode import Episode
from agent.brain.domain.event import IngressEvent, OutFrame, SourceClass
from agent.brain.domain.ids import new_turn_id
from agent.brain.domain.thread import Thread, TurnRecord


def _discard(_) -> None:
    return None


class IngressSupport:
    def _record_user(self, event: IngressEvent, thread: Thread, turn_id: str, at: float,
                     engage: str = "") -> None:
        with thread.lock:
            thread.record(TurnRecord(turn_id=turn_id, role="user", text=event.text,
                                     source=event.source, at=at, speaker=event.principal,
                                     addressed_to=event.addressed_to, engage=engage))

    def _record_memory(self, decision: RouteDecision, event: IngressEvent, at: float) -> None:
        record = getattr(self.ports, "record", None)
        if record is None:
            return
        try:
            record(decision.scope, TurnRecord(
                turn_id=new_turn_id(), role="user", text=event.text, source=event.source,
                at=at, speaker=event.principal, addressed_to=event.addressed_to,
                engage=decision.engage.value),
                participants=event.participants or (event.principal,))
        except Exception as exc:                       # noqa: BLE001
            self.log(f"attention: memory record failed ({type(exc).__name__})")

    def _emit_reaction(self, event: IngressEvent, thread: Thread, turn_id: str,
                       source, decision: RouteDecision) -> None:
        frame = OutFrame(kind="reaction",
                         payload={"emoji": "📝", "text": event.text[:40],
                                  "reason": decision.engage_reason},
                         thread_id=thread.id, turn_id=turn_id, epoch=thread.epoch + 1)
        try:
            self._sink_for(source)(frame)
        except Exception as exc:                       # noqa: BLE001
            self.log(f"attention: reaction frame failed ({type(exc).__name__})")

    def _note_presence(self, event: IngressEvent, at: float) -> None:
        if self.entities is None:
            return
        entity, _ = self.router.resolve_entity(event)
        who = event.principal or (
            "self" if getattr(event.origin, "value", "") == "self" else "")
        self.entities.mark_presence(entity, event.source, event.scene or "together",
                                    event.source_class, at, principal=who)

    def _remember_presence(self, decision: RouteDecision, thread: Thread, at: float) -> None:
        if self.entities is None:
            return
        who = decision.principal or ("self" if decision.key.scope.startswith("self") else "")
        self.entities.mark_presence(decision.entity, decision.source, decision.key.scene,
                                    decision.lane, at, principal=who)
        # 環境源只更新在場，不搶注意力；旁聽同理：聽見了，但不轉頭。
        if decision.lane is not SourceClass.AMBIENT and decision.engage is Engagement.REPLY:
            self.entities.attend(decision.entity, thread.id, at, principal=who)

    def _maybe_seal(self, thread: Thread, decision: RouteDecision, at: float,
                    incoming: str) -> Episode | None:
        if self.episodes is None:
            return None
        last_active = at - max(0.0, decision.idle_s)
        reason = self.episodes.cut_if_needed(thread, at, incoming=incoming,
                                             last_active=last_active)
        return self.episodes.seal(thread, at, reason) if reason is not None else None

    def _sink_for(self, source) -> Callable:
        if source is not None:
            return source.deliver
        return self.sink or _discard
