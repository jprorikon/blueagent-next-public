"""源（Source）協議：**只做傳輸 ↔ 事件，不做人格、不做記憶、不做大腦**。

模組化的硬邊界（使用者要求「源和管道要模組化」）：

    Source  認識自己的傳輸格式、自己的能力、自己綁定的角色實體；
            它把原始輸入正規化成 `IngressEvent`，把 `OutFrame` 寫回自己的傳輸。
    Router  決定這件事是哪個實體、哪條線、哪條管線、要不要排隊／搶佔。
    Pipeline 決定這一輪做什麼（門檻→記憶→人格→腦→工具→聲音→回傳）。
    Runtime 決定誰能跑、跑幾個、誰先跑、誰被打斷。

因此：**源不准 import 大腦／記憶／人格模組，也不准自己起執行緒**。
新增一個源 = 寫一個 `BaseSource` 子類 + 在組合根註冊，不需要改 router 或 runtime。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from agent.brain.domain.event import (ConversationMode, IngressEvent, InputKind, Origin,
                             OutFrame, SourceClass)


@dataclass(frozen=True, slots=True)
class SourceCaps:
    """一個源「能做什麼」。路由與管線靠它選路，不靠 `if source == "discord"`。"""

    text_in: bool = True
    text_out: bool = True
    audio_in: bool = False
    audio_out: bool = False
    image_in: bool = False
    interruptible: bool = False
    presence: bool = False
    reactions: bool = False

    def supports(self, kind: InputKind | str) -> bool:
        key = kind.value if isinstance(kind, InputKind) else str(kind)
        return {
            "text": self.text_in,
            "audio": self.audio_in,
            "image": self.image_in,
            "signal": True,
            "presence": self.presence,
        }.get(key, False)


@runtime_checkable
class Source(Protocol):
    id: str
    cls: SourceClass
    entity: str
    scene: str
    caps: SourceCaps

    def normalize(self, raw: Mapping[str, Any], *, meta: Mapping[str, str] | None = None) -> IngressEvent:
        ...

    def deliver(self, frame: OutFrame) -> None:
        ...

    def close(self) -> None:
        ...

    def health(self) -> Mapping[str, Any]:
        ...


class BaseSource:
    """預設實作：子類通常只需要改 `id` / `cls` / `caps`，必要時覆寫 `normalize`。

    `entity` 是這個源**預設**指向的角色實體；單一入口（例如手機 app）可以在事件裡
    用 `entity` 或舊欄位 `soul` 覆蓋。源自己不做「該不該換角色」的判斷——那是 router 的事。
    """

    kind = "base"

    def __init__(self, source_id: str, cls: SourceClass, *, entity: str = "",
                 scene: str = "together", pipeline: str = "",
                 principal: str = "owner", conversation: ConversationMode = ConversationMode.DIRECT,
                 ownerless: bool = False,
                 resolver: Callable[[Mapping[str, str]], str] | None = None,
                 caps: SourceCaps | None = None) -> None:
        self.id = str(source_id or self.kind).strip()
        self.cls = cls
        self.entity = str(entity or "").strip()
        self.scene = str(scene or "together").strip()
        self.pipeline = str(pipeline or "").strip()
        self.owner = str(principal or "owner").strip()
        self.conversation = conversation
        self.ownerless = bool(ownerless)
        self.resolver = resolver
        self.caps = caps or SourceCaps()

    def principal_for(self, meta: Mapping[str, str] | None = None) -> str:
        """這條輸入是誰給的。

        **只認 `meta`（由 HTTP 層認證後填入的信任欄位），不讀 payload。**
        單主人階段回 `owner`；多 principal 時由 `resolver`（通常包著
        `PrincipalRegistry.resolve`）用 token／Discord user id／TRPG 席位解析。
        """
        if self.ownerless:
            return ""
        if self.resolver is not None:
            return str(self.resolver(meta or {}) or self.owner).strip()
        return str((meta or {}).get("principal") or self.owner).strip() or "owner"

    def normalize(self, raw: Mapping[str, Any], *, meta: Mapping[str, str] | None = None) -> IngressEvent:
        payload = dict(raw or {})
        kind_raw = str(payload.get("kind") or ("text" if payload.get("text") else "signal")).strip().lower()
        try:
            kind = InputKind(kind_raw)
        except ValueError:
            kind = InputKind.SIGNAL
        entity = str(payload.get("entity") or payload.get("soul") or self.entity or "").strip()
        scene = str(payload.get("scene") or self.scene or "").strip()
        # 群場景：同一條線、多人發言；一對一：各自一條線。
        origin = Origin.SELF if self.ownerless else Origin.OWNED
        if origin is Origin.SELF:
            scope = f"self:{scene}"
        elif self.conversation is ConversationMode.GROUP:
            scope = f"group:{scene}"
        else:
            scope = ""
        return IngressEvent(
            principal=self.principal_for(meta),
            entity=entity,
            source=self.id,
            kind=kind,
            at=float(payload.get("at") or time.time()),
            source_class=self.cls,
            origin=origin,
            reactions=self.caps.reactions,
            scope=scope,
            scene=scene,
            thread_hint=str(payload.get("thread") or payload.get("thread_hint") or "").strip(),
            pipeline_hint=str(payload.get("pipeline") or self.pipeline or "").strip(),
            addressed_to=str(payload.get("addressed_to")
                             or ("entity" if payload.get("mention_entity")
                                 or payload.get("reply_to_entity") else "")).strip(),
            participants=tuple(str(who).strip() for who in
                               (payload.get("participants") or payload.get("room") or ())
                               if str(who).strip()),
            trace_id=str(payload.get("trace") or payload.get("trace_id")
                         or (meta or {}).get("trace") or "").strip(),
            idempotency_key=str(payload.get("idempotency_key") or payload.get("id")
                                or "").strip(),
            payload=payload,
            trace={k: str(v) for k, v in (meta or {}).items()},
        )

    def deliver(self, frame: OutFrame) -> None:    # noqa: ARG002 - 預設不送（測試／只讀源）
        return None

    def close(self) -> None:
        return None

    def health(self) -> Mapping[str, Any]:
        return {"id": self.id, "kind": self.kind, "class": self.cls.label,
                "entity": self.entity, "scene": self.scene, "pipeline": self.pipeline,
                "principal": self.owner, "conversation": self.conversation.value,
                "ownerless": self.ownerless,
                "caps": asdict(self.caps)}
