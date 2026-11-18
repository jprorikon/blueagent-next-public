"""切句：**silero-vad**（照搬現役 `ears.StreamCutter`，owner 2026-10-22：「STT 復用之前那套」）。

為什麼不用能量門檻（我第一版就是能量）：能量分不出「電視／風扇／鍵盤」和「人在說話」。
現役量過的事實（`blueagent/kws/exp/stream_cut_check.py`，15 s 人工串流＝房間音 ×6 倍音量 ＋ 他的話）：

  · **silero-vad**：抓到他兩段，放大的房間音**沒有**被當語音；CPU 43 ms / 15 s ≈ 0.3% 一核。
  · ten-vad：把他的第一句和後面的房間音併成一段 ⇒ 不用。
  · pyannote segmentation：離線固定窗，拿它當切點會漂 ⇒ 只當分話者標籤。

三個必須照抄的細節（都是現役踩出來的）：

  1. **前置 preroll（預設 500 ms）**：silero 要幾百毫秒才確認「這是語音」，段落起點偏晚（實測 0.7 s），
     不補的話起音會被吃掉。所以自己留一份環形緩衝回填。
  2. **環形緩衝要留「最長一句＋前置」**：只留 preroll+2 s 的話前置永遠補不到（第一版實測＝沒補）。
  3. **硬上限（預設 8 s）**：silero 只在「靜音後」才切；電視不停嘴時段長無界（離線 45 s 素材切出 13.2 s／
     20.7 s，真機 17–31 s）。自己在語音中累積到上限就 flush。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

#: 現役同一顆（~640 KB）。
DEFAULT_VAD_MODEL = Path(
    "/opt/blueagent-next/blueagent/kws/models/vad/silero_vad.onnx")

SR = 16000


class SileroCutter:
    """吃 16 kHz mono int16 的塊，回「閉合的句子」（已含前置）。"""

    def __init__(self, model: Path | str | None = None, *,
                 threshold: float = 0.5, min_silence_ms: int = 500,
                 min_speech_ms: int = 250, max_speech_ms: int = 12000,
                 preroll_ms: int = 500, buffer_s: float = 30.0,
                 hard_speech_ms: int = 8000, on_speech_start=None) -> None:
        import sherpa_onnx
        model_path = Path(model or os.environ.get("BLUEAGENT_VAD_MODEL") or DEFAULT_VAD_MODEL)
        if not model_path.is_file():
            raise FileNotFoundError(f"silero VAD 模型不在：{model_path}")
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(model_path)
        config.silero_vad.threshold = threshold
        config.silero_vad.min_silence_duration = min_silence_ms / 1000.0
        config.silero_vad.min_speech_duration = min_speech_ms / 1000.0
        config.silero_vad.max_speech_duration = max_speech_ms / 1000.0
        config.sample_rate = SR
        self.vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=buffer_s)
        self.preroll = int(preroll_ms * SR / 1000)
        self.keep_ring = self.preroll + int(max_speech_ms * SR / 1000) + SR
        self.ring = np.zeros(0, dtype=np.int16)
        self.ring_base = 0
        self.total = 0
        self.closed = 0
        self.hard_limit = int(max(hard_speech_ms, 0) * SR / 1000)
        self.voiced_run = 0
        self.last_spans: list[tuple[int, int]] = []
        #: 語音**開始**那一刻的回呼（打斷用；現在只有「一句結束」的回呼得不夠快）。
        self.on_speech_start = on_speech_start
        self._in_speech = False
        self.speech_starts = 0

    # ── 對外 ────────────────────────────────────────────────────────────────
    def feed(self, chunk: np.ndarray) -> list[np.ndarray]:
        if chunk.size == 0:
            return []
        chunk = np.ascontiguousarray(chunk, dtype=np.int16)
        self.ring = np.concatenate([self.ring, chunk]) if self.ring.size else chunk.copy()
        if self.ring.size > self.keep_ring:
            drop = self.ring.size - self.keep_ring
            self.ring = self.ring[drop:]
            self.ring_base += drop
        self.total += len(chunk)
        self.vad.accept_waveform(chunk.astype(np.float32) / 32768.0)
        self._note_edge()
        if self.hard_limit > 0:
            if self.vad.is_speech_detected():
                self.voiced_run += len(chunk)
                if self.voiced_run >= self.hard_limit:
                    self.vad.flush()
                    self.voiced_run = 0
            else:
                self.voiced_run = 0
        return self._drain()

    def _note_edge(self) -> None:
        """silero 說「現在有語音」而上一格還沒有 ⇒ 這是**說話的開始**（上升沿）。

        打斷（barge-in）必須在「他剛開口」就反應，不能等這句講完（那是 `_drain()` 的事）。
        這裡只看 silero 的即時判定，不改它的切句行為。
        """
        active = bool(self.vad.is_speech_detected())
        if active and not self._in_speech:
            self.speech_starts += 1
            if self.on_speech_start is not None:
                try:
                    self.on_speech_start()
                except Exception:                      # noqa: BLE001 - 回呼壞了不該斷音訊
                    pass
        self._in_speech = active

    def flush(self) -> list[np.ndarray]:
        self.vad.flush()
        self.voiced_run = 0
        return self._drain()

    def health(self) -> dict:
        return {"model": "silero", "closed": self.closed, "samples": self.total,
                "speech_starts": self.speech_starts, "in_speech": self._in_speech}

    # ── 內部 ────────────────────────────────────────────────────────────────
    def _drain(self) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        self.last_spans = []
        while not self.vad.empty():
            segment = self.vad.front
            self.vad.pop()
            out.append(self._audio(segment))
            self.last_spans.append((int(segment.start),
                                    int(segment.start) + len(segment.samples)))
            self.closed += 1
        return out

    def _audio(self, segment) -> np.ndarray:
        audio = np.clip(np.asarray(segment.samples, dtype=np.float32) * 32768.0,
                        -32768, 32767).astype(np.int16)
        head = segment.start - self.preroll
        if head >= self.ring_base and head < self.ring_base + len(self.ring):
            prefix = self.ring[head - self.ring_base:segment.start - self.ring_base]
            if prefix.size:
                audio = np.concatenate([prefix, audio])
        return audio


__all__ = ["DEFAULT_VAD_MODEL", "SR", "SileroCutter"]
