"""Router：把一個 `IngressEvent` 變成一個 `RouteDecision`。

它是**唯一**決定「哪個實體、哪條線、哪條管線、什麼模式、什麼優先級」的地方。
HTTP handler、源、管線都不准自己算這些。

實體優先（使用者 2026-10-21 定調）：先解析 `entity`，再談場景。未知實體落到 default，
並且在 route 理由裡留痕——不靜默改成別的角色。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

from agent.brain.domain.decision import Engagement, RouteDecision
from agent.brain.domain.entity import EntityRegistry
from agent.brain.domain.event import IngressEvent, Origin, SourceClass
from agent.brain.domain.ids import ThreadKey
from agent.brain.pipelines import blueprints
from agent.brain.ports import ThreadStorePort
from agent.brain.router import rules


@dataclass(frozen=True, slots=True)
class RouterConfig:
    windows: Mapping[SourceClass, float] = field(default_factory=lambda: dict(rules.WINDOWS))
    #: 首幀預算（毫秒）。預設沿用 `rules.DEADLINES`；單人聊天接真模型時 8 s 太短
    #: （2026-10-21 實測 qwen38-27b 一句要 6–8 s，第一句常被砍掉）。
    deadlines: Mapping[SourceClass, int | None] = field(
        default_factory=lambda: dict(rules.DEADLINES))
    #: 文本层的唤醒词（除了实体 id/name 之外）。群聊里没被点到时她应该安静。
    wake_words: tuple[str, ...] = ()
    #: `addressed_to=group`（@所有人/对房间说）时要不要接。默认旁观，避免抢话。
    reply_to_group: bool = False

    def window(self, cls: SourceClass) -> float:
        return float(self.windows.get(cls, 600.0))

    def deadline(self, cls: SourceClass) -> int | None:
        return self.deadlines.get(cls)


class Router:
    def __init__(self, threads: ThreadStorePort, *, entities: EntityRegistry | None = None,
                 pipelines: frozenset[str] | None = None, config: RouterConfig | None = None,
                 classifier: Callable[[IngressEvent], str | None] | None = None) -> None:
        self._threads = threads
        self._entities = entities
        self._pipelines = pipelines
        self._config = config or RouterConfig()
        self._classifier = classifier

    # ── 實體解析：整個模型的第一性問題 ────────────────────────────────────────────
    def resolve_entity(self, event: IngressEvent) -> tuple[str, str]:
        """回 `(entity_id, note)`。`note` 非空＝發生了 fallback，要進日誌。"""
        candidate = str(event.entity or "").strip()
        if self._entities is None:
            return candidate or "default", ""
        if candidate:
            found = self._entities.get(candidate)
            if found is not None:
                return found.id, ""
            default = self._entities.default()
            if default is not None:
                return default.id, f"entity-unknown({candidate})->{default.id}"
            return candidate, "entity-unknown(no-default)"
        default = self._entities.default()
        if default is not None:
            return default.id, "entity-default"
        return "default", "entity-default(none)"

    def _wake(self, event: IngressEvent, entity: str) -> bool:
        """她自己的名字（实体 id/name）或配置的唤醒词出现在文本里。群聊兜底用。"""
        text = event.text.casefold()
        if not text:
            return False
        names = {entity.casefold(), entity.replace("-", " ").casefold()}
        if self._entities is not None:
            found = self._entities.get(entity)
            if found is not None and found.name:
                names.add(found.name.casefold())
        # 只認**這個實體自己的**叫名（實體上有就用實體的，沒有才退回全域設定）。
        # 全域 wake_words 含別的角色名，拿它判 ⇒ 喊「ノリ」會叫醒 mira（2026-10-22 修）。
        nicknames = tuple(getattr(found, "nicknames", ()) or ()) if self._entities is not None else ()
        if nicknames:
            names.update(word.casefold() for word in nicknames if word)
        else:
            names.update(word.casefold() for word in self._config.wake_words if word)
        return any(name and name in text for name in names)

    def route(self, event: IngressEvent, *, now: float | None = None) -> RouteDecision:
        at = float(now if now is not None else (event.at or time.time()))
        entity, entity_note = self.resolve_entity(event)
        scene = rules.scene_for(event)
        purpose = rules.purpose_for(event)
        # 一對一：錨＝說話的人；群場景：group:<scene>；無主源：self:<scene>。
        if event.scope:
            anchor = event.scope
        elif event.origin is Origin.SELF:
            anchor = f"self:{event.source}"
        else:
            anchor = event.principal or "owner"
        anchor = anchor.strip() or "owner"
        key = ThreadKey(scope=anchor, entity=entity, scene=scene, purpose=purpose.value)

        hinted = self._threads.get(event.thread_hint) if event.thread_hint else None
        if hinted is not None and hinted.key.scope == key.scope and hinted.entity == key.entity:
            thread = hinted
            created = False
            hit = "explicit"
            previous_active = thread.last_active_at
            with thread.lock:
                thread.touch(at)
        else:
            attached = self._threads.attach(key, now=at, window=self._config.window(event.source_class),
                                            lane=event.source_class)
            thread = attached.thread
            created = attached.created
            hit = "new" if created else "continuity"
            previous_active = attached.previous_active

        group = event.scope.strip().lower().startswith("group:")
        engage = rules.engage_for(event, group=group,
                                  wake=self._wake(event, entity),
                                  reply_to_group=self._config.reply_to_group)
        mode = rules.choose_mode(event, thread)
        pipeline, why = rules.pipeline_for(event, purpose, classifier=self._classifier)
        if self._pipelines is not None and pipeline not in self._pipelines:
            fallback = blueprints.default_pipeline(event.source_class, event.kind, purpose.value)
            why = f"{why}:unknown({pipeline})->{fallback}"
            pipeline = fallback

        with thread.lock:
            thread.sources.add(event.source)
            people = [person for person in (event.participants or (event.principal,)) if person]
            thread.participants.update(people)
            thread.lane = min(thread.lane, event.source_class)

        idle_s = max(0.0, at - float(previous_active or at))
        who = ("self" if event.origin is Origin.SELF
               else (event.principal if event.principal == key.scope
                     else f"{event.principal}@{key.scope}"))
        target = f" to={event.addressed_to}" if event.addressed_to else ""
        reason = (f"who={who}{target} src={event.source} hit={hit} idle={idle_s:.0f}s "
                  f"pipeline={why} scene={scene}" + (f" {entity_note}" if entity_note else ""))
        return RouteDecision(
            thread_id=thread.id,
            key=key,
            pipeline=pipeline,
            lane=event.source_class,
            priority=rules.priority_for(event.source_class, purpose),
            mode=mode,
            scope=thread.memory_scope(event.principal),
            deadline_ms=self._config.deadline(event.source_class),
            reason=reason,
            created=created,
            at=at,
            idle_s=idle_s,
            source=event.source,
            principal=event.principal,
            engage=engage,
            addressed_to=event.addressed_to,
        )


def route_once(threads: ThreadStorePort, event: IngressEvent, **kwargs) -> RouteDecision:
    """便利函式：一次性建立 Router 再路由（測試與工具用）。"""
    return Router(threads, **kwargs).route(event)
