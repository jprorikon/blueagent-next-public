"""工具的共同型別：呼叫情境、結果、協議與註冊表（按模式／身分／能力／渠道收窄；分層見 `channels.py`）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from agent.brain.runtime.tools.channels import CALL_ALLOW, channel_of
from agent.brain.runtime.voice import VoiceService


@dataclass(frozen=True, slots=True)
class ToolContext:
    entity: str
    principal: str
    thread_id: str
    source: str
    voice: VoiceService
    #: chat / work —— 委任类工具只在 work 出现（owner 2026-10-21）
    mode: str = "chat"
    #: 这个 principal 是不是主人（guest 一律 False）
    is_owner: bool = True
    #: 这一轮被放行的能力名（PersonaStage 算）：`delegate` 等。空集＝只有默认工具。
    capabilities: frozenset = frozenset()
    #: 這一輪**在場的人**（群聊裡不只說話的那一位）；空＝只有她和這個 principal
    present: frozenset = frozenset()


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    frames: list[dict] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "data": dict(self.data),
                "frames": list(self.frames), "error": self.error}


class Tool(Protocol):
    name: str
    schema: dict

    def run(self, ctx: ToolContext, arguments: dict) -> ToolResult:
        ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        #: schema 上的函式名 → registry 名。模型回的是 schema 名（`memory_search`），
        #: 這裡登記的是 `memory.search`——2026-10-24 前只認後者 ⇒ 真模型一叫就 `unknown_tool`。
        self._aliases: dict[str, str] = {}
        #: 優先送到模型面前的工具（名字集合）。**順序會影響模型選不選它**——
        #: 實測（2026-10-29）`sticker.send` 排在第 11 位時模型 **12/12 輪都不調**，
        #: 排到最前面（或只給 6 個工具）時 **每次都調** ⇒ 排序不是美觀問題，是功能問題。
        self._priority: list[str] = []

    def register(self, tool: Tool) -> "ToolRegistry":
        self._tools[tool.name] = tool
        schema = getattr(tool, "schema", None) or {}
        function = str((schema.get("function") or {}).get("name") or "")
        if function and function != tool.name:
            self._aliases[function] = tool.name
        for old in getattr(tool, "aliases", ()) or ():   # 改名後的舊名（歷史裡的呼叫照樣認得）
            self._aliases.setdefault(str(old), tool.name)
        return self

    def prioritize(self, *names: str) -> "ToolRegistry":
        """把這幾個工具排到給模型的清單**前面**（其餘相對順序不變）。"""
        for name in names:
            if name and name not in self._priority:
                self._priority.append(name)
        return self

    def get(self, name: str) -> Tool | None:
        """按 registry 名找；找不到再按 schema 函式名找（registry 名優先，別名不會蓋掉真名）。"""
        key = str(name)
        tool = self._tools.get(key)
        if tool is None and key in self._aliases:
            tool = self._tools.get(self._aliases[key])
        return tool

    def allowed(self, tool: Tool, *, mode: str = "chat", is_owner: bool = True,
                capabilities: Iterable[str] = (), source: str | None = None) -> bool:
        """工具面按模式/身分/能力/渠道收窄：`modes` 沒寫＝所有模式都能用；`channels` 沒寫＝所有渠道。

        `source=None`＝呼叫端不看渠道（自主工作那條有自己的白名單：`runtime/self_work.py`）。
        """
        channel = channel_of(source) if source is not None else ""
        if channel == "call" and tool.name not in CALL_ALLOW:
            return False
        channels = getattr(tool, "channels", ())
        if channels and source is not None and channel not in channels:
            return False
        modes = getattr(tool, "modes", ())
        if modes and str(mode or "chat") not in modes:
            return False
        # 逐模式的工具白名單（MCP server 用 `tools_by_mode` 宣告）：聊天裡只留她真的會用的那幾支
        filters = getattr(tool, "name_filters", {}) or {}
        allowed_names = filters.get(str(mode or "chat"))
        if allowed_names is not None:
            remote = getattr(tool, "remote_name", "")
            if remote and remote not in allowed_names:
                return False
        if getattr(tool, "owner_only", False) and not is_owner:
            return False
        capability = getattr(tool, "capability", "")
        if capability and capability not in set(capabilities or ()):
            return False
        return True

    def run(self, name: str, arguments: dict, ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(ok=False, error=f"unknown_tool:{name}")
        if not self.allowed(tool, mode=ctx.mode, is_owner=ctx.is_owner,
                            capabilities=ctx.capabilities, source=ctx.source):
            return ToolResult(ok=False, error=f"tool_not_allowed:{name}")
        try:
            return tool.run(ctx, dict(arguments or {}))
        except Exception as exc:                       # noqa: BLE001 - 工具失敗不該炸整輪
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def snapshot(self) -> list[str]:
        return sorted(self._tools)

    def available(self, name: str, *, mode: str = "chat", is_owner: bool = True,
                  capabilities: Iterable[str] = (), source: str | None = None) -> bool:
        """這個渠道／模式／身分**能不能用**這支工具（不管是不是按需解鎖）——技能索引拿它判斷要不要列。"""
        tool = self.get(name)
        return tool is not None and self.allowed(tool, mode=mode, is_owner=is_owner,
                                                 capabilities=capabilities, source=source)

    def canonical(self, name: str) -> str:
        """schema 函式名／registry 名 → registry 名（不認得 ⇒ `""`）。"""
        tool = self.get(name)
        return tool.name if tool is not None else ""

    def schemas(self, *, mode: str = "chat", is_owner: bool = True,
                capabilities: Iterable[str] = (), entity: str = "",
                source: str = "", unlocked: Iterable[str] = ()) -> list[dict]:
        """這一輪給模型看的工具。有 `schema_for` 的工具按角色／來源現算（`None`＝這一輪不給）。

        `on_demand` 的工具只有在 `unlocked`（這一輪 `skill.view` 載入的技能所需）裡才給（`tools/channels.py`）。
        """
        unlocked = set(unlocked or ())
        out: list[dict] = []
        for tool in self._tools.values():
            if not self.allowed(tool, mode=mode, is_owner=is_owner, capabilities=capabilities,
                                source=source):
                continue
            if getattr(tool, "on_demand", False) and tool.name not in unlocked:
                continue
            schema_for = getattr(tool, "schema_for", None)
            schema = (schema_for(entity=entity, source=source) if callable(schema_for)
                      else getattr(tool, "schema", None))
            if schema:
                out.append((tool.name, schema))
        # ⚠️ **優先工具排前面**（2026-10-29 實測）：模型對清單裡靠後的工具視而不見
        # （`sticker.send` 排第 11 位時 12/12 輪都不調；排前面就每次都調）。
        if self._priority:
            head = [pair for pair in out if pair[0] in self._priority]
            tail = [pair for pair in out if pair[0] not in self._priority]
            out = head + tail
        return [schema for _, schema in out]


__all__ = ["ToolContext", "ToolResult", "Tool", "ToolRegistry"]
