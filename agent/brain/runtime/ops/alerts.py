"""告警：**她出問題時要有人知道**（`turns.jsonl` ＋ `/api/metrics` 只是紀錄，沒人盯）。

三條會真的影響體驗的訊號（其餘先不做，避免變成監控系統）：

| 訊號 | 門檻（預設） | 為什麼 |
|---|---|---|
| p95 一輪延遲 | > 16 s（且樣本 ≥ 20） | 超過這個她「聽起來像壞了」 |
| 語音失敗率 | > 15% | `speech_error` 佔比高＝嘴壞掉（TTS 掛了） |
| 一輪失敗率 | > 15% | `error` 佔比高＝腦或工具炸了 |

門檻不是拍的：2026-10-22 用 `tools/alert_calibration.py` 掃 473 輪真數據標定 ——
實測 p95 = 8.05 s、p99 = 13.4 s、語音失敗 1.4%、單輪失敗 0.4%，
門檻取「實測 × 3（或 p99 × 1.2）」，這樣只在**真的壞掉**時叫，不在日常波動裡叫。

紀律：
  · **同一種告警有冷卻**（預設 30 分鐘），不然她一壞就被私聊刷屏；
  · 只在樣本足夠時才判（一輪失敗不算故障）；
  · 送出失敗不重試（下一輪檢查再說）；
  · 冷卻狀態**落盤**（`data/alerts.json`）：重啟不該讓她立刻再報一次同一件事。
"""

from __future__ import annotations

import json
import pathlib
import threading
import time

#: 多久檢查一次（秒）。
DEFAULT_INTERVAL_S = 300.0
#: 同一種告警的冷卻（秒）。
DEFAULT_COOLDOWN_S = 1800.0
#: 至少要有這麼多輪才判（避免一輪失敗就報警）。
DEFAULT_MIN_TURNS = 20


class AlertMonitor:
    def __init__(self, recorder, send, *, interval_s: float = DEFAULT_INTERVAL_S,
                 cooldown_s: float = DEFAULT_COOLDOWN_S,
                 latency_p95_ms: float = 16_000.0,
                 speech_error_rate: float = 0.15,
                 failure_rate: float = 0.15,
                 min_turns: int = DEFAULT_MIN_TURNS,
                 state_path=None, log=None, clock=time.time) -> None:
        self.recorder = recorder
        self.send = send
        self.interval_s = max(5.0, float(interval_s))
        self.cooldown_s = max(0.0, float(cooldown_s))
        self.latency_p95_ms = float(latency_p95_ms)
        self.speech_error_rate = float(speech_error_rate)
        self.failure_rate = float(failure_rate)
        self.min_turns = max(1, int(min_turns))
        self._path = pathlib.Path(state_path) if state_path is not None else None
        self._log = log or (lambda _message: None)
        self._clock = clock
        self._lock = threading.RLock()
        self._last: dict[str, float] = {}
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.sent = 0
        self.checks = 0
        self._load()

    # ── 生命週期 ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, daemon=True, name="ba-alerts")
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # 啟動先等一輪：剛起來的數字沒有意義（也避免重啟就報警）
        while not self._stop.wait(self.interval_s):
            try:
                self.check()
            except Exception as exc:                       # noqa: BLE001 - 監控不能炸服務
                self._log(f"alerts: 檢查失敗（{type(exc).__name__}: {exc}）")

    # ── 判定 ────────────────────────────────────────────────────────────────
    def evaluate(self) -> list[dict]:
        """回「現在該報的」告警（不含冷卻判斷，方便測試）。"""
        metrics = self.recorder.metrics() if self.recorder is not None else {}
        turns = int(metrics.get("turns") or 0)
        if turns < self.min_turns:
            return []
        problems: list[dict] = []
        p95 = float((metrics.get("latency_ms") or {}).get("p95") or 0.0)
        if p95 > self.latency_p95_ms:
            problems.append({"kind": "latency_p95",
                             "text": f"一輪回覆變慢了：p95 {p95/1000:.1f}s（門檻 {self.latency_p95_ms/1000:.0f}s，"
                                     f"{turns} 輪樣本）"})
        speech_errors = int(metrics.get("speech_errors") or 0)
        if turns and speech_errors / turns > self.speech_error_rate:
            problems.append({"kind": "speech_error_rate",
                             "text": f"我的聲音出問題了：{speech_errors}/{turns} 輪合成失敗"
                                     f"（>{self.speech_error_rate:.0%}），文字還是會送出去"})
        failures = int(metrics.get("failures") or 0)
        if turns and failures / turns > self.failure_rate:
            problems.append({"kind": "turn_failure_rate",
                             "text": f"我这边一直在出错：{failures}/{turns} 轮失败"
                                     f"（>{self.failure_rate:.0%}）"})
        return problems

    def check(self) -> list[dict]:
        """跑一次：該報的就送（受冷卻限制），回這次真的送出去的。"""
        now = float(self._clock())
        self.checks += 1
        sent: list[dict] = []
        for problem in self.evaluate():
            kind = str(problem.get("kind") or "")
            with self._lock:
                last = self._last.get(kind)
                if last is not None and (now - last) < self.cooldown_s:
                    continue
                self._last[kind] = now
                self._save_locked()
            text = "⚠️ " + str(problem.get("text") or "")
            try:
                ok = bool(self.send(text))
            except Exception as exc:                       # noqa: BLE001
                ok = False
                self._log(f"alerts: 送出失敗（{type(exc).__name__}: {exc}）")
            if ok:
                self.sent += 1
                sent.append(problem)
                self._log(f"alerts: 已私聊主人（{kind}）")
            else:
                # 送不出去就把冷卻退回，下一輪再試
                with self._lock:
                    self._last.pop(kind, None)
        return sent

    def snapshot(self) -> dict:
        with self._lock:
            return {"checks": self.checks, "sent": self.sent,
                    "cooldown_s": self.cooldown_s,
                    "last": {kind: round(at, 3) for kind, at in self._last.items()}}

    # ── 冷卻狀態落盤 ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return
        if isinstance(raw, dict):
            for kind, value in (raw.get("last") or {}).items():
                try:
                    self._last[str(kind)] = float(value)
                except (TypeError, ValueError):
                    continue

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last": self._last,
                                       "saved_at": round(time.time(), 3)},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass


__all__ = ["AlertMonitor"]
