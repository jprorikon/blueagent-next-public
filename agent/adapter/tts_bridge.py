"""真聲音的嘴：**直接包現役橋的 `ba/voice`**（不另寫一套合成客戶端）。

owner 2026-10-21 定：「TTS 走我們的合成」——實例池、熱機親和、第一句優先、片假名正規化、
分句規則（`Sentences.kt` 的孿生）全部照舊沿用現役那份，新倉只在外面包一層 adapter。

於是這個檔的工作只有三件：

   1. 把橋的 `ba/voice/tts.py`（＋`pool.py`）載進來（`BLUEAGENT_TTS_BRIDGE_ROOT`）；
   2. 逐句合成 → 換成新倉的 clip（`audio`/`seconds`/`motion`/`shape`）；
   3. 失敗語義：拋 `TtsError`，**上層照樣把文字送出去**。

設定**沿用橋的環境變數名**（`TTS_URL`／`TTS_URLS`／`TTS_POOL`／`TTS_REMOTE_ROOT`／
`TTS_TEXT_LANG`／`TTS_TIMEOUT_S`／`VOICE_ALIASES`）——同一件事只有一處可改。
新倉只多兩個開關：`BLUEAGENT_TTS=bridge` 與 `BLUEAGENT_TTS_BRIDGE_ROOT=<橋 repo 路徑>`。

⚠️ 分層：只有組合根可以 import 這個檔（見 `adapters/__init__.py`）。
⚠️ `sys.path` 代價：`ba` 這個套件名一旦載入就固定指向某個 root；一個進程只接一個橋 root。
"""

from __future__ import annotations

import base64
import importlib
import io
import pathlib
import sys
import time
import wave
from dataclasses import dataclass, field
from typing import Mapping

#: 一次合成最多幾句（防守：模型話癆時不要無限合成）。
MAX_SENTENCES = 24


class TtsError(RuntimeError):
    pass


#: 允許的朗讀語言。owner 2026-10-21 定「鎖 ja」⇒ 預設 ja，且只認這兩個值。
TEXT_LANGS = ("ja", "zh")


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    bridge_root: pathlib.Path
    assets: pathlib.Path
    url: str = ""
    remote_root: str = ""
    pool: str = ""
    urls: Mapping[str, str] = field(default_factory=dict)
    #: 朗讀語言，**鎖 ja**：她的聲線是日文庫；逐句改判會讓同一則回覆換語言。
    text_lang: str = "ja"
    speed: float = 1.0
    #: 一句合成的上限。**不要給到 120 s**：實例卡死時（2026-10-22 遇到 :9881 只接 TCP 不回 HTTP）
    #: 整輪會被拖到 deadline、一句話都回不出來。20 s 之後就放棄、回 speech_error，
    #: 文字照樣送出（現役的紀律：嘴壞了不能連話都不說）。
    timeout_s: float = 20.0
    hop_ms: int = 64
    aliases: Mapping[str, str] = field(default_factory=dict)
    #: 要不要算嘴型腳本（`motion`）。算不出來只是沒有嘴型，不影響出聲。
    motion: bool = True


@dataclass(frozen=True, slots=True)
class BridgeClip:
    """一句話的合成結果。`payload()` 的鍵＝`speech` 幀的 payload。"""

    text: str
    wav: bytes
    seconds: float
    index: int
    total: int
    last: bool
    shape: str = ""
    motion: list | None = None
    instance: str = ""
    text_lang: str = ""

    def payload(self) -> dict:
        return {"text": self.text,
                "audio": base64.b64encode(self.wav).decode("ascii"),
                "seconds": self.seconds,
                "index": self.index, "total": self.total, "last": self.last,
                "shape": self.shape, "motion": list(self.motion or []),
                "instance": self.instance, "text_lang": self.text_lang}


def wav_seconds(wav: bytes) -> float:
    """從 wav header 算時長；不是合法 wav 就退回 64 KB/s 粗估（16-bit mono 32 kHz）。"""
    try:
        with wave.open(io.BytesIO(wav), "rb") as handle:
            rate = handle.getframerate() or 0
            if rate:
                return round(handle.getnframes() / float(rate), 3)
    except Exception:                                    # noqa: BLE001 - 不是 wav 就粗估
        pass
    return round(len(wav) / 64000.0, 3)


def load_bridge(root: str | pathlib.Path):
    """載入橋的 `ba.voice.tts` 與 `ba.voice.pool`；回 `(tts, pool_module)`。"""
    base = pathlib.Path(root).expanduser().resolve()
    if not (base / "ba" / "voice" / "tts.py").exists():
        raise TtsError(f"橋不在這個路徑（找不到 ba/voice/tts.py）：{base}")
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    tts = importlib.import_module("ba.voice.tts")
    pool = importlib.import_module("ba.voice.pool")
    return tts, pool


class BridgeTts:
    """`VoiceService.tts` 需要的形狀：`speak(text, character) -> [clip]`。"""

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self._tts = None
        self._pool = None
        self._motion_warned = False
        self._lang_warned = False

    # ── 懶載入（第一次要說話時才 import 橋）─────────────────────────────────
    @property
    def tts(self):
        if self._tts is None:
            tts, pool_module = load_bridge(self.config.bridge_root)
            self._tts = tts
            raw_pool = self.config.pool
            self._pool = (pool_module.from_env(raw_pool, remote_default=self.config.remote_root)
                          if raw_pool else None)
        return self._tts

    @property
    def pool(self):
        _ = self.tts                                        # 觸發載入
        return self._pool

    # ── 對外 ────────────────────────────────────────────────────────────────
    def voice_for(self, character: str) -> str:
        name = str(character or "").strip()
        return str(self.config.aliases.get(name, name)).strip()

    def url_for(self, character: str) -> str:
        name = self.voice_for(character)
        return str(self.config.urls.get(name) or self.config.url).rstrip("/")

    def speak(self, text: str, character: str) -> list[BridgeClip]:
        text = str(text or "").strip()
        if not text:
            return []
        tts = self.tts
        voice = self.voice_for(character)
        try:
            sentences = list(tts.split_for_speech(text))[:MAX_SENTENCES]
            ref = tts.pick_ref(pathlib.Path(self.config.assets), voice)
        except Exception as exc:                            # noqa: BLE001 - 分句/參考音壞了
            raise TtsError(f"準備合成失敗（{type(exc).__name__}: {exc}）") from exc
        if not sentences:
            return []
        clips: list[BridgeClip] = []
        total = len(sentences)
        for index, sentence in enumerate(sentences):
            lang = self._lang_for(sentence)
            self._warn_if_mismatch(sentence, lang)
            wav, used = self._synthesize(sentence, voice, ref,
                                         priority=0 if index == 0 else 1, text_lang=lang)
            motion, why = self._motion(wav)
            if motion is None and why and not self._motion_warned:
                self._motion_warned = True
                print(f"[tts] 嘴型算不出（{why}）— 這句照樣出聲", file=sys.stderr, flush=True)
            clips.append(BridgeClip(text=sentence, wav=wav, seconds=wav_seconds(wav),
                                    index=index, total=total, last=index == total - 1,
                                    shape=str(character), motion=motion, instance=used,
                                    text_lang=lang))
        return clips

    def _warn_if_mismatch(self, sentence: str, lang: str) -> None:
        """文字語言和聲線語言不一致時提醒一次（別讓亂碼悄悄上線）。"""
        if self._lang_warned or lang != "ja":
            return
        hanzi = sum(1 for ch in sentence if "\u4e00" <= ch <= "\u9fff")
        kana = sum(1 for ch in sentence if "\u3040" <= ch <= "\u30ff")
        if hanzi >= 2 and not kana:
            self._lang_warned = True
            print(f"[tts] 這句是中文、聲線是日文庫，會念歪：{sentence[:32]!r}"
                  f" — 生成側語言約束沒生效（檢查 BLUEAGENT_SPEAK_LANG）",
                  file=sys.stderr, flush=True)

    def _lang_for(self, sentence: str) -> str:                     # noqa: ARG002 - 介面保留
        """朗讀語言＝鎖定的那個（預設 ja）。

        曾經有 `auto`（逐句判假名/漢字）：同一則回覆會前後句換語言、聲音也跟著變，
        而且中文台詞用日文庫念出來的東西人耳難判——owner 2026-10-21 定「鎖 ja」，移除。
        文字語言由生成側（`BLUEAGENT_SPEAK_LANG`）保證；這裡只負責不要自己變。
        """
        lock = str(self.config.text_lang or "").strip().lower()
        return lock if lock in TEXT_LANGS else "ja"

    def _synthesize(self, sentence: str, voice: str, ref: dict, *, priority: int,
                    text_lang: str = ""):
        """一句 → `(wav, 出聲的實例網址)`。池在就用池（重試／換台／回退都由池負責）。"""
        tts = self.tts
        kwargs = {"text_lang": text_lang, "speed": self.config.speed,
                  "timeout": self.config.timeout_s}
        pool = self.pool
        started = time.monotonic()
        try:
            if pool is not None:
                return pool.say(sentence, voice, ref, say=tts.say,
                                 fallback=(self.url_for(voice), self.config.remote_root),
                                 priority=priority, **kwargs)
            return (tts.say(sentence, self.url_for(voice), voice, ref,
                            self.config.remote_root, **kwargs),
                    self.url_for(voice))
        except TtsError:
            raise
        except Exception as exc:                            # noqa: BLE001 - 橋的 VoiceError 等
            raise TtsError(f"合成失敗（{type(exc).__name__}: {exc}）") from exc
        finally:
            # 慢句要吵：一句超過 8 秒代表池在排隊／機器被擠，離 20 s 逾時只差一點
            #（owner 2026-10-22 問「為什麼會失敗」，當時沒有這種紀錄可查）
            spent = time.monotonic() - started
            if spent >= 8.0:
                print(f"[tts] 慢句 {spent:.1f}s：{sentence[:24]!r}", file=sys.stderr, flush=True)

    def _motion(self, wav: bytes) -> tuple[list | None, str]:
        if not self.config.motion:
            return None, ""
        try:
            frames, why = self.tts.motion_for(wav, self.config.hop_ms)
        except Exception as exc:                            # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"
        return frames, why

    def health(self) -> dict:
        try:
            pool = self.pool
        except TtsError as exc:
            return {"backend": "bridge", "ok": False, "error": str(exc)}
        if pool is not None:
            report = pool.health()
            report.update({"backend": "bridge", "pool": pool.describe()})
            return report
        url = self.url_for("")
        started = time.monotonic()
        try:
            import urllib.request
            with urllib.request.urlopen(f"{url}/docs", timeout=3.0) as response:
                alive = response.status == 200
        except Exception as exc:                            # noqa: BLE001
            return {"backend": "bridge", "ok": False, "url": url,
                    "error": f"{type(exc).__name__}: {exc}"}
        return {"backend": "bridge", "ok": alive, "url": url,
                "latency_ms": int((time.monotonic() - started) * 1000)}


# ── 從環境變數組裝（沿用的是橋的變數名）───────────────────────────────────────
def _pairs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        if key.strip() and value.strip():
            out[key.strip()] = value.strip()
    return out


def _text_lang(raw) -> str:
    """`TTS_TEXT_LANG` 只認 ja/zh；沒設＝ja（鎖定）。給了別的值直接吵，別靜默降級。"""
    value = str(raw or "").strip().lower()
    if not value:
        return "ja"
    if value not in TEXT_LANGS:
        raise ValueError(f"TTS_TEXT_LANG 只支援 {'/'.join(TEXT_LANGS)}（鎖 ja），收到：{value}")
    return value


def build_tts(env: Mapping[str, str] | None = None):
    import os
    values = os.environ if env is None else env
    backend = str(values.get("BLUEAGENT_TTS") or "off").strip().lower()
    if backend in {"", "off", "none", "mock"}:
        return None
    if backend not in {"bridge", "gptsovits", "gsv"}:
        raise ValueError(f"unknown BLUEAGENT_TTS: {backend}")
    root = str(values.get("BLUEAGENT_TTS_BRIDGE_ROOT") or "").strip()
    assets = str(values.get("TTS_ASSETS") or values.get("BLUEAGENT_TTS_ASSETS") or "").strip()
    if not root or not assets:
        raise ValueError("BLUEAGENT_TTS=bridge 需要 BLUEAGENT_TTS_BRIDGE_ROOT 與 TTS_ASSETS")
    url = str(values.get("TTS_URL") or "").strip()
    pool = str(values.get("TTS_POOL") or "").strip()
    if not url and not pool:
        raise ValueError("BLUEAGENT_TTS=bridge 需要 TTS_URL 或 TTS_POOL（沿用橋的變數）")
    config = BridgeConfig(
        bridge_root=pathlib.Path(root),
        assets=pathlib.Path(assets),
        url=url,
        remote_root=str(values.get("TTS_REMOTE_ROOT") or "").strip(),
        pool=pool,
        urls=_pairs(values.get("TTS_URLS") or ""),
        text_lang=_text_lang(values.get("TTS_TEXT_LANG")),
        speed=float(values.get("TTS_SPEED") or 1.0),
        timeout_s=float(values.get("TTS_TIMEOUT_S") or 20),
        motion=str(values.get("BLUEAGENT_TTS_MOTION") or "1").strip() not in {"0", "off", "false"},
        aliases=_pairs(values.get("VOICE_ALIASES") or ""),
    )
    return BridgeTts(config)


def tts_label(tts) -> str:
    """啟動日誌用的一行。"""
    if tts is None:
        return "mock"
    config = getattr(tts, "config", None)
    where = getattr(config, "pool", "") or getattr(config, "url", "") or "?"
    return f"bridge {where}"


__all__ = ["BridgeClip", "BridgeConfig", "BridgeTts", "MAX_SENTENCES", "TtsError",
           "build_tts", "load_bridge", "tts_label", "wav_seconds"]
