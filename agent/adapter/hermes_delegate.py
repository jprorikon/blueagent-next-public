"""委任的傳輸層：把一件工作交給 Hermes（`/v1/runs` 轉播），與現役橋同一條路。

为什么包这一层而不是自己造：现役 blueagent 的委任就是「開一條 run → 轉播事件 → 代理審批 →
收 run.completed 的 output」。协议（`POST /v1/runs` → `GET /v1/runs/<id>/events` →
`POST /v1/runs/<id>/approval`）已经跑了几百轮；这里只做协议，不做业务判断（命名/回执/剥路径
在 `runtime/delegate.py`）。

事件词汇（实测）：
    message.delta     正文增量（`delta`）
    reasoning.available 思考（`text`）
    tool.started/completed  她的工具（`tool` / `preview` / `error`）
    approval.*        审批闸
    run.completed     收尾（`output` 是整段正文）

審批代答：轉播的 run 是無人值守的，沒人按「同意」就會卡在 `waiting_for_approval`
（现役实测卡过 6 分钟）。所以這裡預設 `approval="allow"`，且第一次答可能早於闸门注册而
被拒 ⇒ 隔一段時間重試。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator

# 委任結果的型別歸 runtime（領域那側）；這支 adapter 只負責把 Hermes 的回覆翻成它
from agent.brain.runtime.delegate import DelegateOutcome  # noqa: F401  (也給舊 import 用)




class HermesGateway:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 900.0,
                 http_timeout: float = 30.0, approval: str = "allow",
                 poll_s: float = 2.0,
                 log: Callable[[str], None] | None = None) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.timeout = float(timeout)
        self.http_timeout = float(http_timeout)
        self.approval = str(approval or "allow").strip().lower()
        self.poll_s = max(0.2, float(poll_s))
        self._log = log or (lambda _message: None)

    # ── 基本請求 ────────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, payload: dict | None = None,
                 *, accept: str = "application/json", timeout: float | None = None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": accept}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base_url}{path}", data=data,
                                         headers=headers, method=method)
        return urllib.request.urlopen(request, timeout=timeout or self.http_timeout)

    def _json(self, method: str, path: str, payload: dict | None = None) -> dict:
        with self._request(method, path, payload) as response:
            raw = response.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}

    # ── 生命週期 ────────────────────────────────────────────────────────────
    def start(self, task: str, *, session_id: str = "",
              history: list[dict] | None = None) -> str:
        payload: dict = {"input": str(task)}
        if session_id:
            payload["session_id"] = str(session_id)
        if history:
            payload["conversation_history"] = list(history)
        started = self._json("POST", "/v1/runs", payload)
        run_id = started.get("run_id") or started.get("id")
        if not run_id:
            raise RuntimeError(f"no run_id in response: {json.dumps(started)[:200]}")
        return str(run_id)

    def status(self, run_id: str) -> str:
        try:
            return str(self._json("GET", f"/v1/runs/{run_id}").get("status") or "")
        except Exception:                              # noqa: BLE001 - 查不到就當還在跑
            return ""

    def answer_approval(self, run_id: str, choice: str) -> dict:
        return self._json("POST", f"/v1/runs/{run_id}/approval", {"choice": choice})

    def relay(self, run_id: str, *, timeout: float | None = None) -> Iterator[tuple[str, dict]]:
        """事件流：事件名在 JSON 的 `.event`（不是 SSE 的 `event:` 行）。

        `timeout` 會變成 socket 的讀取上限 ⇒ 上游**卡住不發事件**時也能收手
        （只靠「每收到一個事件才檢查 deadline」是會被靜默卡死的）。
        """
        with self._request("GET", f"/v1/runs/{run_id}/events",
                           accept="text/event-stream", timeout=timeout) as upstream:
            for raw in upstream:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    frame = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                yield str(frame.get("event") or ""), frame

    # ── 一次委任 ────────────────────────────────────────────────────────────
    def run(self, task: str, *, session_id: str = "",
            on_event: Callable[[str, dict], None] | None = None,
            deadline: float | None = None) -> DelegateOutcome:
        started_at = time.monotonic()
        limit = float(deadline if deadline is not None else self.timeout)
        outcome = DelegateOutcome()
        stop = threading.Event()

        def watch() -> None:
            """代理審批：無人值守的 run 不能卡在闸门。"""
            if self.approval in {"", "none", "manual", "off"}:
                return
            polls = 0
            while not stop.wait(self.poll_s):
                polls += 1
                status = self.status(outcome.run_id)
                if status in {"completed", "failed", "canceled", "cancelled",
                              "stopped", "error"}:
                    return
                if status != "waiting_for_approval":
                    continue
                if polls % 5 != 1:
                    continue
                try:
                    answer = self.answer_approval(outcome.run_id, self.approval)
                    self._log(f"delegate: 代答審批 choice={self.approval} "
                              f"resolved={answer.get('resolved')}")
                except Exception as exc:               # noqa: BLE001 - 答不上不該結束轉播
                    self._log(f"delegate: 代答失敗 {type(exc).__name__}: {exc}")

        try:
            outcome.run_id = self.start(task, session_id=session_id)
        except Exception as exc:                        # noqa: BLE001
            outcome.error = f"start failed: {type(exc).__name__}: {exc}"
            outcome.elapsed = time.monotonic() - started_at
            return outcome
        watcher = threading.Thread(target=watch, daemon=True, name="delegate-approval")
        watcher.start()
        pieces: list[str] = []
        try:
            # 讀取上限＝剩下的 deadline：這樣「上游卡住不發事件」也會在 deadline 附近收手
            remaining = max(0.5, limit - (time.monotonic() - started_at))
            for event, frame in self.relay(outcome.run_id, timeout=remaining):
                outcome.events.append({"event": event, "at": round(time.monotonic() - started_at, 3)})
                if on_event is not None:
                    on_event(event, frame)
                if event == "message.delta":
                    pieces.append(str(frame.get("delta") or ""))
                elif event in {"tool.started", "tool.completed"}:
                    preview = str(frame.get("preview") or "")
                    if preview:
                        outcome.previews.append(preview)
                elif event in {"run.completed", "run.failed", "run.canceled",
                               "run.stopped", "run.error"}:
                    outcome.status = event.split(".", 1)[1]
                    outcome.text = str(frame.get("output") or "".join(pieces))
                    break
                if time.monotonic() - started_at > limit:
                    outcome.error = f"delegate timeout after {limit:.0f}s"
                    break
            if not outcome.status and not outcome.error:
                outcome.error = "event stream ended unexpectedly"
                outcome.text = "".join(pieces)
        except Exception as exc:                        # noqa: BLE001
            if time.monotonic() - started_at >= limit:
                outcome.error = f"delegate timeout after {limit:.0f}s"
            else:
                outcome.error = f"relay failed: {type(exc).__name__}: {exc}"
        finally:
            stop.set()
            outcome.elapsed = time.monotonic() - started_at
        return outcome

    def health(self) -> dict:
        try:
            with self._request("GET", "/health") as response:
                return {"ok": response.status == 200, "url": self.base_url}
        except Exception as exc:                        # noqa: BLE001
            return {"ok": False, "url": self.base_url,
                    "error": f"{type(exc).__name__}: {exc}"}


__all__ = ["DelegateOutcome", "HermesGateway"]
