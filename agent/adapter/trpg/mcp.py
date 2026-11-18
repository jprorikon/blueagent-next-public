"""桌子的 MCP 協議：看牌面／出手／說話（`table_view`／`table_act`／`table_say`）。"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import urllib.request

from agent.adapter.trpg.config import TrpgConfig


class TrpgMcp:
    """最小 MCP 客戶端（`initialize` → `tools/call`）。協議與現役 harness 一致。"""

    def __init__(self, config: TrpgConfig, *, timeout: float = 60.0) -> None:
        self.config = config
        self.timeout = float(timeout)
        self.url = f"{config.hub_url}/mcp"
        self._id = 0
        self._lock = threading.Lock()
        self._initialized = False

    def ensure(self) -> None:
        """`initialize` **延後到第一次真的要用**。

        啟動時還沒有桌與 session（它們由 kick 帶進來），那時打 `/mcp` 會被 hub 回 400
        （`X-Table-Id` 是空的）——所以不能在建構就握手，2026-10-22 實測踩到。
        """
        if self._initialized:
            return
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "blueagent-next", "version": "0"}})
        self._initialized = True          # 握手成功才記（以前先記 ⇒ 第一次失敗就永遠不再握手）

    def _token(self) -> str:
        # ① 直接給值（放 `.secrets/trpg.env`，不必把 trpg 目錄掛進容器）
        direct = os.environ.get("BLUEAGENT_TRPG_HUB_TOKEN") or ""
        if str(direct).strip():
            return str(direct).strip()
        # ② 讀檔（hub 產生的 `logs/hub.token`；要掛進容器）
        path = self.config.token_file
        if not path:
            return ""
        try:
            return pathlib.Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _rpc(self, method: str, params: dict) -> dict:
        if method != "initialize" and not self._initialized:
            self.ensure()
        with self._lock:
            self._id += 1
            body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                         method="POST")
        for key, value in (("Content-Type", "application/json"),
                           ("Accept", "application/json, text/event-stream"),
                           ("X-Hub-Token", self._token()),
                           ("X-Table-Id", self.config.table_id),
                           ("X-Table-Session", self.config.session),
                           ("X-Table-GM-Token", self.config.gm_token)):
            request.add_header(key, value)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read().decode()
        for line in raw.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return json.loads(raw or "{}")

    def call(self, tool: str, **args) -> dict:
        args = {key: value for key, value in args.items() if value is not None}
        args.setdefault("session", self.config.session)
        out = self._rpc("tools/call", {"name": tool, "arguments": args})
        result = out.get("result") or {}
        for item in result.get("content") or ():
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except Exception:                              # noqa: BLE001
                    return {"text": item["text"]}
        return result or out

    def view(self, seat: str | None = None) -> dict:
        found = self.call("table_view", seat=seat or self.config.seat, detail="full")
        return found.get("view") or found

    def act(self, action: str, *, target=None, note=None, skill=None, item=None) -> dict:
        return self.call("table_act", seat=self.config.seat, action=action,
                         target=target, note=note, req=None, skill=skill, item=item)

    def say(self, text: str) -> dict:
        return self.call("table_say", seat=self.config.seat, text=text)
