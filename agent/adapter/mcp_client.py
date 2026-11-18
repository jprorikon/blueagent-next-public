"""最小 MCP 客戶端（P5）：**外部能力走協議接進來**，不寫死 API。

兩種傳輸：

  · `stdio`：啟動子程序（例如本機 firecrawl 代理），用 stdin/stdout 走 JSON-RPC
  · `http`：POST JSON-RPC 到 `http(s)://…`（streamable HTTP 的簡化版，不帶 SSE）

協議面只做三件事：`initialize` → `tools/list` → `tools/call`。
失敗語義一律「回錯誤、不拋」——外部能力掛掉不該擋她說話（跟 TTS 壞掉同一條規矩）。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, *, name: str, transport: str = "stdio", command=None, args=(),
                 env=None, url: str = "", timeout_s: float = 30.0, cwd=None) -> None:
        self.name = str(name)
        self.transport = str(transport or "stdio").lower()
        self.command = command
        self.args = [str(item) for item in (args or ())]
        self.env = {str(k): str(v) for k, v in (env or {}).items()}
        self.url = str(url or "")
        self.timeout_s = float(timeout_s)
        self.cwd = cwd
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._next_id = 0
        self._initialized = False
        self.last_error = ""

    # ── 生命週期 ────────────────────────────────────────────────────────────
    def start(self) -> bool:
        if self.transport != "stdio":
            return True
        if self._proc is not None and self._proc.poll() is None:
            return True
        if not self.command:
            self.last_error = "no_command"
            return False
        environ = dict(os.environ)
        environ.update(self.env)
        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1, env=environ, cwd=self.cwd)
        except (OSError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        return True

    def close(self) -> None:
        proc, self._proc = self._proc, None
        self._initialized = False
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:                                  # noqa: BLE001
            try:
                proc.kill()
            except Exception:                              # noqa: BLE001
                pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:                              # noqa: BLE001
                pass

    # ── JSON-RPC ────────────────────────────────────────────────────────────
    def _post_http(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            body = response.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise MCPError(f"bad_json: {body[:120]}") from exc

    def _roundtrip_stdio(self, payload: dict) -> dict:
        if not self.start() or self._proc is None or self._proc.stdin is None:
            raise MCPError(self.last_error or "not_started")
        with self._lock:
            try:
                self._proc.stdin.write(json.dumps(payload) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise MCPError(f"stdio_write: {type(exc).__name__}") from exc
            deadline = time.time() + self.timeout_s
            while time.time() < deadline:
                line = self._proc.stdout.readline() if self._proc.stdout else ""
                if not line:
                    if self._proc.poll() is not None:
                        raise MCPError("server_exited")
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue                          # 伺服器雜訊（log）跳過
                if message.get("id") == payload.get("id") or message.get("id") is None:
                    return message
            raise MCPError("timeout")

    def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method,
                   "params": dict(params or {})}
        try:
            message = (self._roundtrip_stdio(payload) if self.transport == "stdio"
                       else self._post_http(payload))
        except MCPError:
            raise
        except Exception as exc:                            # noqa: BLE001
            raise MCPError(f"{type(exc).__name__}: {exc}") from exc
        if "error" in message:
            error = message["error"] or {}
            raise MCPError(str(error.get("message") or error))
        return message.get("result") or {}

    def initialize(self) -> dict:
        result = self.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "blueagent-next", "version": "1.0"},
        })
        self._initialized = True
        return result

    def list_tools(self) -> list[dict]:
        if not self._initialized:
            try:
                self.initialize()
            except MCPError as exc:
                self.last_error = str(exc)
                return []
        try:
            result = self.call("tools/list", {})
        except MCPError as exc:
            self.last_error = str(exc)
            return []
        tools = result.get("tools") if isinstance(result, dict) else None
        return [tool for tool in (tools or []) if isinstance(tool, dict)]

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self.call("tools/call", {"name": str(name),
                                          "arguments": dict(arguments or {})})
        return result if isinstance(result, dict) else {"content": result}
