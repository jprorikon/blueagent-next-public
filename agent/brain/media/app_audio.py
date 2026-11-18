"""送給 App 的語音瘦身（owner 2026-10-30「網路傳輸優化」）。

每一句的語音是整段 WAV、base64 塞進 SSE 的 `speech` 幀：合成器若出 32 kHz／16-bit／單聲道，
一秒約 64 KB、base64 後 85 KB ⇒ 二十秒的回覆就 1.7 MB，行動網路上回覆越長越慢出聲。
手機喇叭聽不出 16 kHz 和 32 kHz 的差別 ⇒ 送給 App 之前降到 `BLUEAGENT_APP_AUDIO_RATE`（預設 16000，0＝不降）。

只動送給 App 的那一份：Discord 語音房、outbox 合併存檔用的是原本的 frame（這裡一律複製，不改原物件）。
降頻用 `audioop.ratecv`（帶一階平滑，比直接抽樣少一點疊頻）；沒有 audioop（Python 3.13 起移除）就原樣送。
"""

from __future__ import annotations

import base64
import io
import os
import wave

try:                                               # Python 3.13 起沒有 audioop；image 是 3.11
    import audioop  # type: ignore[import-not-found]  # noqa: F401
except ImportError:                                # pragma: no cover - 依 Python 版本
    audioop = None


def target_rate() -> int:
    try:
        return max(0, int(os.environ.get("BLUEAGENT_APP_AUDIO_RATE") or 16000))
    except ValueError:
        return 16000


def slim_wav(wav: bytes, rate: int | None = None) -> bytes:
    """WAV 降到 `rate`（只降不升；不是 16-bit PCM WAV、或已經夠低 ⇒ 原樣回）。"""
    rate = target_rate() if rate is None else int(rate)
    if not wav or rate <= 0 or audioop is None:
        return wav
    try:
        with wave.open(io.BytesIO(wav), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            original = source.getframerate()
            frames = source.readframes(source.getnframes())
    except (wave.Error, EOFError):
        return wav
    if width != 2 or original <= rate:
        return wav
    converted, _ = audioop.ratecv(frames, width, channels, original, rate, None, 1, 1)
    out = io.BytesIO()
    with wave.open(out, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(converted)
    return out.getvalue()


def mulaw_wav(wav: bytes) -> bytes:
    """16-bit PCM WAV → G.711 μ-law WAV；不認得的格式就原樣回。

    這是**可協商**的 `mulaw-v1`：16 kHz 單聲道語音的 PCM 位元組再減半，base64 後
    也減半。WAV 仍由端末自己的解碼器展開，不依賴 iOS／Android 的 AAC／Opus 差異。
    """
    if not wav or audioop is None:
        return wav
    try:
        with wave.open(io.BytesIO(wav), "rb") as source:
            channels = source.getnchannels()
            rate = source.getframerate()
            count = source.getnframes()
            width = source.getsampwidth()
            frames = source.readframes(count)
    except (wave.Error, EOFError):
        return wav
    if width != 2 or channels <= 0 or rate <= 0:
        return wav
    data = audioop.lin2ulaw(frames, 2)
    # Python `wave` 只能寫 PCM，因此手工寫標準 fmt(7)＋fact＋data 區塊。
    import struct
    fmt = struct.pack("<HHIIHHH", 7, channels, rate, rate * channels, channels, 8, 0)
    chunks = (b"fmt " + struct.pack("<I", len(fmt)) + fmt
              + b"fact" + struct.pack("<II", 4, count)
              + b"data" + struct.pack("<I", len(data)) + data)
    if len(data) % 2:
        chunks += b"\0"
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def transport_wav(wav: bytes, profile: str = "") -> bytes:
    """送給端末的表示：先降頻；明確協商 `mulaw-v1` 時再壓成 G.711。"""
    slim = slim_wav(wav)
    return mulaw_wav(slim) if str(profile or "").strip() == "mulaw-v1" else slim


def slim_speech_event(event: str, payload: dict, profile: str = "") -> dict:
    """SSE 的 `speech` 幀：降頻並按協商壓縮（回**新的** dict；其他事件原樣回）。"""
    if event != "speech" or not isinstance(payload, dict):
        return payload
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else None
    holder = inner if inner is not None else payload
    encoded = holder.get("audio")
    if not isinstance(encoded, str) or not encoded:
        return payload
    try:
        wav = base64.b64decode(encoded)
    except (ValueError, TypeError):
        return payload
    slim = transport_wav(wav, profile)
    if slim is wav:
        return payload
    fresh = dict(holder, audio=base64.b64encode(slim).decode("ascii"))
    if str(profile or "").strip() == "mulaw-v1" and slim is not wav:
        fresh["audio_codec"] = "mulaw"
    return dict(payload, payload=fresh) if inner is not None else fresh


__all__ = ["mulaw_wav", "slim_speech_event", "slim_wav", "target_rate", "transport_wav"]
