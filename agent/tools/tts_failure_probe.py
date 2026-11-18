"""查「為什麼合成失敗」：池的健康、單句延遲、並發時會不會拿不到實例。

三個數字：
  ① 每台 TTS 實例健康（`/docs` 探活）
  ② 一句 40 字日語要多久（timeout 是 20 s，接近就危險）
  ③ 三句同時要（模擬她講長回覆＋心跳同時觸發）會不會 `沒有可用實例`
"""
import os
import sys
import time

os.environ.setdefault("TTS_ASSETS", "/bridge-assets")
os.environ.setdefault("TTS_REMOTE_ROOT", "/home/developer/gsv-voices")

from agent.adapter.tts_bridge import build_tts  # noqa: E402

tts = build_tts(os.environ)
print("bridge:", type(tts).__name__, "url:", getattr(getattr(tts, "config", None), "url", "?"),
      "timeout:", getattr(getattr(tts, "config", None), "timeout_s", "?"))

health = tts.health()
print("health :", {k: health.get(k) for k in ("backend", "ok", "error")})
for inst in (health.get("instances") or health.get("pool", {}).get("instances") or []):
    print("  實例:", {k: inst.get(k) for k in ("url", "alive", "busy", "held")})

sentence = "霧の隙間の下、暗くて底が見えないでしょう、それに鉄のにおいがさっきより近いのが気にかかってるんだ。"
print("pool   :", getattr(getattr(tts, "pool", None), "describe", lambda: "-")())
started = time.monotonic()
try:
    clips = tts.speak(sentence, "mira")
    print(f"單句 : {time.monotonic() - started:.2f}s / {len(clips)} 段 / "
          f"{sum(clip.seconds for clip in clips):.1f}s 音檔")
except Exception as exc:
    print(f"單句 : 失敗 {type(exc).__name__}: {exc}（{time.monotonic() - started:.2f}s）")

import threading

results: list[str] = []


def fire(index: int) -> None:
    began = time.monotonic()
    try:
        clips = tts.speak(f"{sentence}（{index}）", "mira")
        results.append(f"#{index} ok {time.monotonic() - began:.1f}s {len(clips)} 段")
    except Exception as exc:                              # noqa: BLE001
        results.append(f"#{index} 失敗 {type(exc).__name__}: {exc}（{time.monotonic() - began:.1f}s）")


threads = [threading.Thread(target=fire, args=(i,)) for i in range(3)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print("並發3句:")
for row in results:
    print("  ", row)

# 失敗轉移：把第一台換成死掉的埠，看是不是自動換到第二台
print("\n=== 失敗轉移（第一台故意指到死埠）===")
os.environ["TTS_POOL"] = ("http://192.168.50.36:9999|/home/developer/gsv-voices;"
                          "http://192.168.50.36:9882|/home/developer/gsv-voices")
pooled = build_tts(os.environ)
began = time.monotonic()
try:
    clips = pooled.speak("転移のテストだよ。", "mira")
    print(f"結果 : ok {time.monotonic() - began:.1f}s / 用到的實例 "
          f"{[getattr(clip, 'instance', '?') for clip in clips]}")
except Exception as exc:
    print(f"結果 : 失敗 {type(exc).__name__}: {exc}")
