"""告警：TTS 失敗率／p95 超閾值就私聊主人（紀錄有人看才算可觀測）。

從 `server/main.main()` 拆出來（2026-10-25）。只在 Discord 連上時啟用（要有人收）。
"""

from __future__ import annotations

import os
import pathlib


def _log(message: str) -> None:
    print(f"  {message}", flush=True)


def make_owner_sender(source):
    """先私聊主人；私聊送不出去才貼到頻道。"""

    def send(text: str) -> bool:
        sent = False
        try:
            sent = bool(source.send_dm("owner", text))
        except Exception:                     # noqa: BLE001
            sent = False
        if not sent:
            sent = bool(source.send_text(text))
        return sent

    return send


def start_alerts(state, source) -> None:
    try:
        from agent.brain.runtime.ops.alerts import AlertMonitor
        recorder = getattr(state.app, "telemetry", None)
        if recorder is None:
            return
        base = pathlib.Path(recorder.path).parent.parent
        alert = AlertMonitor(
            recorder, make_owner_sender(source),
            interval_s=float(os.environ.get("BLUEAGENT_ALERT_INTERVAL_S") or 300),
            cooldown_s=float(os.environ.get("BLUEAGENT_ALERT_COOLDOWN_S") or 1800),
            # 門檻由 `tools/alert_calibration.py` 用真數據標定（2026-10-22）
            latency_p95_ms=float(os.environ.get("BLUEAGENT_ALERT_P95_MS") or 16000),
            speech_error_rate=float(
                os.environ.get("BLUEAGENT_ALERT_SPEECH_FAIL")
                or os.environ.get("BLUEAGENT_ALERT_SPEECH_RATE") or 0.15),
            failure_rate=float(
                os.environ.get("BLUEAGENT_ALERT_TURN_FAIL")
                or os.environ.get("BLUEAGENT_ALERT_FAIL_RATE") or 0.15),
            state_path=base / "alerts.json",
            log=_log)
        state.app.alerts = alert
        alert.start()
        print("  alerts:  每 "
              f"{alert.interval_s:.0f}s 檢查一次（p95>{alert.latency_p95_ms/1000:.0f}s／"
              f"失敗率>{alert.speech_error_rate:.0%} ⇒ 私聊主人）", flush=True)
    except Exception as exc:                          # noqa: BLE001 - 告警不該擋開機
        print(f"  alerts: 啟動失敗（{type(exc).__name__}: {exc}）", flush=True)


__all__ = ["make_owner_sender", "start_alerts"]
