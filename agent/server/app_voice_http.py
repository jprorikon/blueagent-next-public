"""App Server → brain 内部语音 HTTP 边界。"""

from __future__ import annotations

import hmac
import os


def handle(handler) -> None:
    """校验独立共享密钥；未配置时默认关闭，不套用终端用户 token。"""
    expected = str(os.environ.get("BLUEAGENT_APP_VOICE_TOKEN") or "")
    supplied = str(handler.headers.get("X-App-Voice-Token") or "")
    if not expected:
        handler._json(503, {"error": "app_voice_unconfigured"})
        return
    if not supplied or not hmac.compare_digest(expected, supplied):
        handler._json(403, {"error": "forbidden"})
        return
    try:
        body = handler._body()
    except ValueError:
        handler._json(400, {"error": "bad_request"})
        return
    from agent.adapter import app_voice
    try:
        handler._json(*app_voice.handle(handler._state(), body))
    except Exception as exc:  # noqa: BLE001 - 内部 pipe 不泄漏堆栈
        handler._json(500, {"error": "app_voice_" + type(exc).__name__})
