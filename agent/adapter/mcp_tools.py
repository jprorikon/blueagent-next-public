"""把 MCP server 的工具註冊成她的工具面（P5）。

設定檔 `data/mcp.json`：

    {"servers": [
      {"name": "firecrawl", "transport": "stdio", "command": "python3",
       "args": ["tools/mcp_firecrawl_server.py"],
       "env": {"FIRECRAWL_URL": "http://localhost.local:3002",
               "FIRECRAWL_API_KEY": "fc-self-hosted"},
       "modes": ["chat", "work"], "owner_only": false, "timeout_s": 60}
    ]}

規則（跟工作技能同一套口徑）：

  · `modes` 決定這台 server 的能力在哪個模式出現（`chat` 就是聊天也能用）
  · `owner_only` 只給「只有主人能做」那類（例如委任）
  · `on_demand`（預設 true）：平常不放進給模型的清單，要某個技能 `requires: [tool:mcp_…]` 載入後才出現；
    `channels`（選填）：只在這些渠道出現（`app`／`call`／`discord`／`discord_vc`／`trpg`）。
    分層的理由見 `runtime/tools/channels.py`
  · 工具名一律 `mcp_<server>_<tool>`（點號消毒成底線，DeepSeek 不收帶點的名字）
  · server 連不上 ⇒ 那批工具**不存在**（不是每次呼叫才失敗），但不影響其它工具
"""

from __future__ import annotations

import json
import pathlib
import re

from agent.adapter.mcp_client import MCPClient, MCPError
from agent.brain.runtime.tools.base import ToolResult

_SAFE = re.compile(r"[^a-zA-Z0-9_]")


def tool_name_for(server: str, tool: str) -> str:
    return "mcp_" + _SAFE.sub("_", f"{server}_{tool}")


def _content_text(result: dict) -> str:
    parts = []
    for item in result.get("content") or ():
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(part for part in parts if part).strip()


class MCPTool:
    """一支被包成「她的工具」的 MCP 工具（延遲啟動、失敗不炸）。"""

    def __init__(self, *, server: str, client: MCPClient, remote_name: str,
                 description: str, schema: dict, modes=(), owner_only: bool = False,
                 on_demand: bool = True, channels=(), inject=(),
                remote_name_filters: dict | None = None) -> None:
        self.server = str(server)
        self.client = client
        self.remote_name = str(remote_name)
        self.name = tool_name_for(self.server, self.remote_name)
        self.modes = tuple(modes or ())
        self.owner_only = bool(owner_only)
        self.on_demand = bool(on_demand)
        self.channels = tuple(channels or ())
        #: 從呼叫端注入的參數（例：`entity`）——MCP server 不知道「現在是誰」，
        #: 由應用側把當前角色塞進去，模型看不到也不可偽造（owner 2026-11-03）
        self.inject = tuple(str(key) for key in (inject or ()))
        #: 每個模式只放行哪些工具（空＝不限）
        self.name_filters = {str(mode): tuple(str(n) for n in names)
                             for mode, names in (remote_name_filters or {}).items()}
        self.schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": str(description or f"{self.server}:{self.remote_name}")[:800],
                "parameters": schema if isinstance(schema, dict) else {"type": "object",
                                                                       "properties": {}},
            },
        }

    def run(self, ctx, arguments: dict) -> ToolResult:
        payload = dict(arguments or {})
        for key in self.inject:
            value = getattr(ctx, key, "")
            # 呼叫端給不出值就拒絕，不能退回去用模型自己填的（那等於讓她指定別人的台帳）
            if value in (None, ""):
                return ToolResult(ok=False, error=f"missing_{key}")
            payload[key] = value
        try:
            result = self.client.call_tool(self.remote_name, payload)
        except MCPError as exc:
            return ToolResult(ok=False, error=f"mcp_error:{exc}")
        except Exception as exc:                            # noqa: BLE001
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        text = _content_text(result)
        if result.get("isError"):
            return ToolResult(ok=False, error=text or "mcp_tool_error", data={"text": text})
        return ToolResult(ok=True, data={"text": text, "server": self.server,
                                         "tool": self.remote_name})


def load_servers(path) -> list[dict]:
    try:
        body = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = body.get("servers") if isinstance(body, dict) else None
    return [server for server in (servers or []) if isinstance(server, dict)]


def build_tools(path, *, cwd=None, log=None) -> tuple[list[MCPTool], list[dict]]:
    """讀設定 → 為每台 server 建 client → 列出工具 → 包成 MCPTool。

    回 `(tools, servers_status)`；`servers_status` 給啟動日誌用（哪台連上、幾個工具、錯誤）。
    """
    tools: list[MCPTool] = []
    status: list[dict] = []
    for server in load_servers(path):
        name = str(server.get("name") or "").strip()
        if not name:
            continue
        client = MCPClient(name=name,
                           transport=str(server.get("transport") or "stdio"),
                           command=server.get("command"),
                           args=server.get("args") or (),
                           env=server.get("env") or {},
                           url=str(server.get("url") or ""),
                           timeout_s=float(server.get("timeout_s") or 30.0),
                           cwd=str(server.get("cwd") or cwd or "") or None)
        rows = client.list_tools()
        modes = server.get("modes") or ("chat",)
        # 任務台帳與技能管理只對主人有意義；即使舊設定還寫 false，也不能再漏給 guest。
        owner_only = bool(server.get("owner_only") or name in {"tasks", "skills"})
        on_demand = server.get("on_demand", True) is not False
        channels = tuple(server.get("channels") or ())
        inject = tuple(server.get("inject") or ())
        # 逐模式的工具白名單（owner 2026-11-03）：聊天裡只給她真正會用的那幾支，
        # 不然光是工具定義就吃掉 9.5k 字（實測 nori 一輪 16 支 / 32k 字）。
        per_mode = dict(server.get("tools_by_mode") or {})
        for row in rows:
            remote_name = str(row.get("name") or "")
            # 全文入口統一走常駐的 `skill.view`；MCP 那支保留給技能顯式解鎖，不重複佔 baseline。
            tool_on_demand = on_demand or (name == "skills" and remote_name == "view")
            tools.append(MCPTool(server=name, client=client,
                                 remote_name=remote_name,
                                 description=str(row.get("description") or ""),
                                 schema=row.get("inputSchema") or {},
                                 modes=modes, owner_only=owner_only,
                                 on_demand=tool_on_demand, channels=channels, inject=inject,
                                 remote_name_filters={mode: tuple(names)
                                                      for mode, names in per_mode.items()}))
        status.append({"server": name, "tools": len(rows),
                       "error": "" if rows else (client.last_error or "no_tools")})
        if log is not None:
            log(f"mcp[{name}]: {len(rows)} 個工具"
                + ("" if rows else f"（{client.last_error or 'no_tools'}）"))
    return tools, status
