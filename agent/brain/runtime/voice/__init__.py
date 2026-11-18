"""語音子包（2026-11-17，NEXT-STEPS WP-D／架構審核 E5）：通話服務、發言權仲裁、語音房在場、通話尾句扣住。

  · `service.py`     — `VoiceService`：通話 session、粘性綁定、出聲（以前的 `runtime/voice.py`）
  · `arbiter.py`     — `FloorArbiter`：一個實體只有一張嘴
  · `vc_presence.py` — 她在 Discord 語音房的在場決策
  · `speech_hold.py` — 通話裡還沒講完的那句先扣住、跟下一句併

只依賴 domain 與葉子工具 `runtime/text_clean.py`（`test_package_layering` 鎖住，不橫向依賴其他 runtime 職責）。

**舊路徑相容**：`from agent.brain.runtime.voice import VoiceService`（以及 `ENDED_TTL_S`／`speech_text`／`_StreamSpeaker`）照用——
這個包就叫 `voice`，這裡把 `service` 的名字轉出來。其餘三支的舊路徑（`runtime.arbiter` 等）已全倉改到新路徑。
"""

from agent.brain.runtime.voice.service import (  # noqa: F401 - 舊路徑相容出口
    ENDED_TTL_S,
    VoiceService,
    _StreamSpeaker,
    speech_text,
)

__all__ = ["ENDED_TTL_S", "VoiceService", "speech_text"]
