"""請求級的幀收集器：一次 send 一個 sink。

除了收集幀，這裡還記「這一輪的關鍵時刻」（`marks`）。為什麼放在這一層：
所有階段的產出都必須經過 sink，所以在這裡打點**不需要**把時間戳一層層傳過管子。
`AppState.send()` 把 marks 換算成相對這一輪開始的毫秒寫進 `turns.jsonl`：

    at ── 前置（記憶檢索／重排／子代理／佇列等待）──> brain 開跑（`thinking` 幀）
       ── LLM 首字（`delta` 幀）──> 首句音檔（`speech` 幀）──> done

2026-10-24 加：owner 問「首字時間是多少」，當時整段只有一個總數 `ms`，
13 秒的前置完全沒有打點，只能猜。
"""

from __future__ import annotations

import threading
import time


def frame_dict(frame) -> dict:
    kind = frame.kind.value if hasattr(frame.kind, "value") else str(frame.kind)
    return {"kind": kind, "payload": dict(frame.payload), "thread": frame.thread_id,
            "turn": frame.turn_id, "epoch": frame.epoch, "final": frame.final,
            "reply_to": frame.reply_to, "mention": frame.mention}


#: 幀 kind → 要記的時刻名。`thinking` 是 LLM 開跑的第一幀（在這之前全是前置階段）。
_MARK_KINDS = {
    "thinking": "brain",
    "delta": "first_delta",
    "speech": "first_speech",
    "speech_error": "first_speech_error",
    "done": "done",
}


class FrameSink:
    def __init__(self, on_frame=None) -> None:
        self.frames: list[dict] = []
        #: 關鍵時刻（`time.monotonic()`）：只記**第一次**出現的那一刻
        self.marks: dict[str, float] = {}
        self._lock = threading.Lock()
        self._on_frame = on_frame
        #: 這一輪的量測值（工具數／字數…）：`note()` 寫、telemetry 讀，不發給端末
        self._notes: dict = {}

    def _mark_locked(self, kind: str, payload: dict) -> None:
        now = time.monotonic()
        self.marks.setdefault("first", now)
        name = _MARK_KINDS.get(str(kind))
        if name:
            self.marks.setdefault(name, now)
        # 首個**帶音檔**的 speech 幀＝「她真的可以出聲了」那一刻（可能晚於第一個 speech 幀）
        if str(kind) == "speech" and payload.get("audio"):
            self.marks.setdefault("first_audio", now)

    def deliver(self, frame) -> None:
        row = frame_dict(frame)
        with self._lock:
            self.frames.append(row)
            self._mark_locked(row.get("kind") or "", row.get("payload") or {})
        if self._on_frame is not None:
            try:
                self._on_frame(row)
            except Exception:                          # noqa: BLE001 - 推送失败不影响回合
                pass

    def mark(self, name: str) -> None:
        """階段打點（給 telemetry 用，**不發給端末**）。

        管子裡的階段（記憶檢索／子代理／狀態引出）不會吐出任何幀，但「這一輪慢在哪一段」
        全靠它們的時間戳。走 `TurnContext.mark()` → `ctx.sink.__self__.mark()`，
        所以不必把 sink 一路傳進每個 stage。
        """
        key = f"stage:{str(name or '').strip()}"
        if key == "stage:":
            return
        with self._lock:
            self.marks.setdefault("first", time.monotonic())
            self.marks.setdefault(key, time.monotonic())

    def note(self, name: str, value) -> None:
        """記一個量測值（給 telemetry；後寫的蓋前寫的）。"""
        key = str(name or "").strip()
        if not key:
            return
        with self._lock:
            self._notes[key] = value

    def notes(self) -> dict:
        with self._lock:
            return dict(self._notes)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.frames)

    def timing(self, started: float, ended: float | None = None) -> dict:
        """把 marks 換算成「距這一輪開始的毫秒」（`started` 用 `time.monotonic()`）。

        `None`＝這一輪沒出現那一刻（例如沒出聲、沒有 delta）。沒有值就留 None，
        不要用 0 假裝有量到。
        """
        with self._lock:
            marks = dict(self.marks)
        out: dict = {}
        for key, name in (("first_frame_ms", "first"), ("brain_ms", "brain"),
                          ("first_token_ms", "first_delta"),
                          ("first_audio_ms", "first_audio"),
                          ("speech_frame_ms", "first_speech"),
                          ("done_ms", "done")):
            at = marks.get(name)
            out[key] = round((at - started) * 1000) if at is not None else None
        # 階段打點：`stage:recall` → `recall_ms`（同樣是「距這一輪開始」）
        for name, at in marks.items():
            if name.startswith("stage:"):
                out[f"{name.split(':', 1)[1]}_ms"] = round((at - started) * 1000)
        if ended is not None:
            out["total_ms"] = round((ended - started) * 1000)
        return out
