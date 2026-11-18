"""語音轉文字：**沿用現役 blue agent 那套**（owner 2026-10-22：「STT 復用之前 blue agent 的」）。

現役的耳朵不是單純叫一顆模型，而是三段（`ba/ears/` + `ears/ears.py` + `ears/text_fix.py`）：

  ① **切句**：silero VAD（`StreamCutter`），不是能量門檻。
  ② **識別**：**SenseVoice**（sherpa-onnx，`model.int8.onnx`，`use_itn=True`，`language=ja`）。
     ⚠️ 不是 whisper —— 實測同一段音檔：whisper small 聽成「もつき 聞こえてる」，
     SenseVoice 也未必對，但名字那類錯音是 `text_fix` 在修。
  ③ **文本糾錯**：本地 qwen（:11435，關思考）把同音誤認與名字歸一，**fail-open**（修不了就原樣）。

這裡把 ② ③ 搬過來，① 仍由 `discord_voice.UtteranceBuffer` 做（能量＋靜音；接 silero 是下一步）。
引擎可選：`BLUEAGENT_STT_ENGINE=sensevoice|whisper`；沒裝 sherpa 就自動退回 whisper。
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path

#: 現役的 SenseVoice 模型目錄（sherpa-onnx 格式）。
DEFAULT_SENSEVOICE_DIR = Path(
    "/opt/blueagent-next/blueagent/kws/exp/stt_ab/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")

#: 角色名的常見誤認（現役 `text_fix` 的規則表，這裡做成確定性版本：不吃網路也能修）。
NAME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ミラ", ("ミラ", "もつき", "ム月キ", "ムつキ", "ぶつき", "六月", "無月", "無つき",
               "ミラー", "ミラー", "ミラちゃん", "もつきちゃん")),
    ("アスター", ("アスター", "アスターちゃん", "アスターちゃん", "里央", "アスター")),
    ("ノリ", ("ノリ", "ノリちゃん", "ノリ", "圭", "蛍")),
)


def fix_names(text: str) -> str:
    """把名字的誤認拉回正字（確定性，零延遲）。只在「明顯是名字」的位置動手。"""
    out = str(text or "")
    for proper, variants in NAME_RULES:
        for variant in variants:
            if variant in out:
                out = out.replace(variant, proper)
    return out


@dataclass
class Transcript:
    text: str = ""
    language: str = ""
    probability: float = 1.0
    seconds: float = 0.0            # 音檔長度
    engine: str = ""
    duration_s: float = 0.0         # 轉寫本身花多久
    fixed: bool = False
    error: str = ""
    #: 「說完了沒」（`end_judge`）：True＝講完、False＝還沒講完、None＝沒判斷／判不出來
    complete: bool | None = None
    #: 分段耗時（秒）：識別本身／LLM 糾錯／說完判斷（沒做的那段是 None）
    asr_s: float | None = None
    fix_s: float | None = None
    judge_s: float | None = None

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error

    # ── 相容舊欄位（`/api/audio` 與測試吃這兩個名字）────────────────────────
    @property
    def duration(self) -> float:
        return self.seconds

    @property
    def model(self) -> str:
        return self.engine

    def to_dict(self) -> dict:
        return {"text": self.text, "language": self.language,
                "probability": self.probability, "duration": self.seconds,
                "model": self.engine, "fixed": self.fixed,
                "took_s": self.duration_s, "error": self.error,
                "complete": self.complete,
                "timing": {"asr_s": self.asr_s, "fix_s": self.fix_s, "judge_s": self.judge_s}}


@dataclass
class SttConfig:
    engine: str = "sensevoice"          # sensevoice | whisper
    language: str = "ja"
    quant: str = "int8"
    model_dir: str = ""
    whisper_size: str = "small"
    fix_names: bool = True
    #: 文本糾錯（本地 qwen）；空＝不做（省 ~0.3 s）
    fix_url: str = ""
    fix_model: str = "qwen38-27b"
    fix_timeout: float = 20.0

    @classmethod
    def from_env(cls, values=None) -> "SttConfig":
        values = os.environ if values is None else values
        engine = str(values.get("BLUEAGENT_STT_ENGINE") or "sensevoice").strip().lower()
        return cls(
            engine=engine if engine in {"sensevoice", "whisper"} else "sensevoice",
            language=str(values.get("BLUEAGENT_STT_LANG") or values.get("EARS_STT_LANG")
                         or "ja").strip(),
            quant=str(values.get("BLUEAGENT_STT_QUANT") or values.get("EARS_STT_QUANT")
                      or "int8").strip(),
            model_dir=str(values.get("BLUEAGENT_STT_DIR")
                         or values.get("EARS_STT_DIR") or "").strip(),
            whisper_size=str(values.get("BLUEAGENT_STT_WHISPER") or "small").strip(),
            fix_names=str(values.get("BLUEAGENT_STT_FIX_NAMES") or "1").strip()
            not in {"0", "off", "false", "no"},
            fix_url=str(values.get("BLUEAGENT_STT_FIX_URL") or "").strip(),
            fix_model=str(values.get("BLUEAGENT_STT_FIX_MODEL") or "qwen38-27b").strip(),
            fix_timeout=float(values.get("BLUEAGENT_STT_FIX_TIMEOUT") or 20.0),
        )


class LocalSTT:
    """一段音檔 → 一句話。`transcribe(bytes|path)`。"""

    def __init__(self, config: SttConfig | None = None, *, log=None, end_judge=None) -> None:
        self.config = config or SttConfig.from_env()
        self._log = log or (lambda _message: None)
        #: 「說完了沒」判斷器（`adapters/end_judge.EndJudge`）；None＝不判斷
        self.end_judge = end_judge
        self._engine = ""            # 實際用起來的引擎
        self._sense = None
        self._whisper = None
        self._prev = ""              # 上一句（給 LLM 糾錯當語境）
        self.last_error = ""
        self.load_seconds = 0.0

    # ── 引擎 ────────────────────────────────────────────────────────────────
    @property
    def engine(self) -> str:
        return self._engine or self.config.engine

    def _load_sensevoice(self):
        import sherpa_onnx
        directory = Path(self.config.model_dir or DEFAULT_SENSEVOICE_DIR)
        quant = self.config.quant if self.config.quant in {"int8", "fp32"} else "int8"
        model = directory / ("model.onnx" if quant == "fp32" else f"model.{quant}.onnx")
        if not model.is_file():
            raise FileNotFoundError(f"SenseVoice 模型不在：{model}")
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(directory / "tokens.txt"),
            num_threads=3, use_itn=True, language=self.config.language)

    def _load_whisper(self):
        from faster_whisper import WhisperModel
        return WhisperModel(self.config.whisper_size, device="cpu", compute_type="int8")

    # ── 對外 ────────────────────────────────────────────────────────────────
    def transcribe(self, audio, *, language: str = "", suffix: str = ".wav",
                   judge: bool = False) -> Transcript:
        """`judge=True` ⇒ 順便判「說完了沒」（和 LLM 糾錯並行，不疊延遲；只有通話要）。"""
        started = time.monotonic()
        pcm, seconds = self._to_pcm(audio)
        if not pcm:
            return Transcript(error="no_audio", engine=self.engine)
        samples = self._to_float(pcm)
        text = ""
        engine = ""
        error = ""
        if self.config.engine != "whisper":
            try:
                if self._sense is None:
                    self._sense = self._load_sensevoice()
                    self.load_seconds = time.monotonic() - started
                text = self._run_sensevoice(samples)
                engine = "sensevoice"
            except Exception as exc:                      # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                self._log(f"stt: SenseVoice 不可用，退回 whisper（{error[:120]}）")
        if not engine:
            try:
                if self._whisper is None:
                    self._whisper = self._load_whisper()
                    self.load_seconds = time.monotonic() - started
                text = self._run_whisper(pcm, language or self.config.language)
                engine = f"whisper:{self.config.whisper_size}"
                error = ""
            except Exception as exc:                      # noqa: BLE001
                return Transcript(error=f"{type(exc).__name__}: {exc}", engine="none")
        asr_s = round(time.monotonic() - started, 3)
        raw = (text or "").strip()
        fixed_text = fix_names(raw) if self.config.fix_names else raw
        previous = self._prev
        verdict = self._start_judge(fixed_text, previous) if judge else None
        used_fix = False
        fix_s = None
        if self.config.fix_url and fixed_text:
            fix_started = time.monotonic()
            corrected = self._llm_fix(fixed_text, previous)
            fix_s = round(time.monotonic() - fix_started, 3)
            if corrected and corrected != fixed_text:
                fixed_text = corrected
                used_fix = True
        complete, judge_s = verdict() if verdict is not None else (None, None)
        if fixed_text:
            self._prev = fixed_text
        self._engine = engine
        self.last_error = ""
        return Transcript(text=fixed_text, language=self.config.language,
                          probability=1.0, seconds=round(seconds, 2), engine=engine,
                          duration_s=round(time.monotonic() - started, 2), fixed=used_fix,
                          complete=complete, asr_s=asr_s, fix_s=fix_s, judge_s=judge_s)

    def _start_judge(self, text: str, previous: str):
        """在旁邊的執行緒跑「說完了沒」；回一個取結果的函式 `() -> (complete, 秒)`。"""
        judge = self.end_judge
        if judge is None or not text:
            return None
        box: dict = {}

        def run() -> None:
            began = time.monotonic()
            try:
                box["complete"] = judge.judge(text, previous)
            except Exception:                           # noqa: BLE001 - fail-open
                box["complete"] = None
            box["took"] = round(time.monotonic() - began, 3)

        worker = threading.Thread(target=run, daemon=True, name="stt-end-judge")
        worker.start()

        def result():
            timeout = getattr(getattr(judge, "config", None), "timeout_s", 1.5)
            worker.join(timeout=float(timeout) + 0.5)
            return box.get("complete"), box.get("took")
        return result

    # ── 內部 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _to_pcm(audio) -> tuple[bytes, float]:
        """吃 bytes(path/wav/pcm16) 或已解析的 path；一律變成 16 kHz mono s16le。"""
        if isinstance(audio, (bytes, bytearray)):
            blob = bytes(audio)
            if blob[:4] == b"RIFF":
                with wave.open(io.BytesIO(blob), "rb") as clip:
                    rate, channels = clip.getframerate(), clip.getnchannels()
                    frames = clip.readframes(clip.getnframes())
                return LocalSTT._resample(frames, rate, channels), len(frames) / (rate * channels * 2)
            return blob, len(blob) / 32000.0             # 裸 PCM16 mono 16k 的假設
        path = Path(str(audio))
        with wave.open(str(path), "rb") as clip:
            rate, channels = clip.getframerate(), clip.getnchannels()
            frames = clip.readframes(clip.getnframes())
        return LocalSTT._resample(frames, rate, channels), len(frames) / (rate * channels * 2)

    @staticmethod
    def _resample(pcm: bytes, rate: int, channels: int) -> bytes:
        if rate == 16000 and channels == 1:
            return pcm
        import audioop
        if channels > 1:
            pcm = audioop.tomono(pcm, 2, 1.0 / channels, 1.0 / channels)
        if rate != 16000:
            pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 16000, None)
        return pcm

    @staticmethod
    def _to_float(pcm: bytes):
        import numpy as np
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    def _run_sensevoice(self, samples) -> str:
        import numpy as np
        stream = self._sense.create_stream()
        stream.accept_waveform(16000, np.ascontiguousarray(samples))
        self._sense.decode_stream(stream)
        return (stream.result.text or "").strip()

    def _run_whisper(self, pcm: bytes, language: str) -> str:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as clip:
            clip.setnchannels(1)
            clip.setsampwidth(2)
            clip.setframerate(16000)
            clip.writeframes(pcm)
        segments, _info = self._whisper.transcribe(io.BytesIO(buffer.getvalue()),
                                                   language=language or None, beam_size=5)
        return "".join(segment.text for segment in segments).strip()

    def _llm_fix(self, text: str, previous: str) -> str:
        """現役 `ears/text_fix.py` 的同款：只修誤認、不改語意，失敗就原樣（fail-open）。"""
        prompt = (f"前の一文（文脈参考のみ）：{previous}\nこの一文：{text}" if previous else text)
        body = {
            "model": self.config.fix_model,
            "messages": [
                {"role": "system", "content": (
                    "あなたは日本語 ASR の後処理器です。入力は認識された一文です"
                    "（同音の誤認識を含むことがあります）。\n"
                    "名前は必ずカタカナで書きます：ミラ、アスター、ノリ。相手の呼び名は「オーナー」。\n"
                    "例：もつき、聞こえてる？ → ミラ、聞こえてる？\n"
                    "規則：1) 認識の誤りだけを直す（同音・仮名の誤認識、名前、語の破れ）。"
                    "2) **元の句読点と語気はそのまま残す**。追加・削除・書き換えはしない。"
                    "3) 本当にまったく判別できないときだけ空文字列を出力する。"
                    "4) 修正後の一文だけを出力する。")},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 120,
        }
        try:
            request = urllib.request.Request(
                self.config.fix_url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            raw = urllib.request.urlopen(request, timeout=self.config.fix_timeout).read()
            content = json.loads(raw.decode())["choices"][0]["message"]["content"]
            cleaned = re.sub(r"^[\"「『]|[\"」』]$", "", (content or "").strip())
            cleaned = cleaned.splitlines()[0].strip() if cleaned else ""
            # ⚠️ 空回＝「模型認為是雜訊」——寧可留原句也不要吃掉她剛說的話
            #    （2026-10-22 實測：qwen 把「もつき、聞こえてる？」判成雜訊回空字串）
            return cleaned or text
        except Exception as exc:                          # noqa: BLE001
            self._log(f"stt: 文本糾錯失敗（{type(exc).__name__}: {exc}）⇒ 用原樣")
            return text

    @staticmethod
    def pcm16_to_wav(pcm: bytes, *, rate: int = 16000, channels: int = 1) -> bytes:
        """裸 PCM16 → wav（`/api/audio` 收 `.raw` 時用）。"""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as clip:
            clip.setnchannels(channels)
            clip.setsampwidth(2)
            clip.setframerate(rate)
            clip.writeframes(pcm)
        return buffer.getvalue()

    def health(self) -> dict:
        return {"engine": self.engine, "loaded": bool(self._sense or self._whisper),
                "language": self.config.language, "fix_names": self.config.fix_names,
                "llm_fix": bool(self.config.fix_url), "end_judge": self.end_judge is not None,
                "error": self.last_error}


__all__ = ["DEFAULT_SENSEVOICE_DIR", "LocalSTT", "NAME_RULES", "SttConfig", "Transcript",
           "fix_names"]
