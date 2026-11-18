"""心情定位（mood line）：把 AEM 的**數字**翻成一句人話（owner 2026-11-01 指定要做）。

owner 定的鏈（2026-10-31 原話，aster 那邊的實作同名）：
```
AEM（只提供數值／後台）
  > 心情定位（翻譯成人話，給別的地方讀）
  > 內心獨白（匯集當下所有狀態去推演）
  > 其他 session 或前置提示詞
```
本檔只做**第二層**，而且刻意做成兩段分離（aster 2026-11-01 的分類：能搬、改數字不改結構）：

- `label`＝詞表翻的短標（`affect.mood()["label"]`，便宜、穩定，給程式比對／死區用）；
- `read`＝**一次 LLM 呼叫**寫的一句「方向・強度・為什麼」，給人與別的 session 讀。

快取是三條一起成立才沿用（少一條就會錯，aster 那裡的實測）：
  ① `|Δv| < 0.010` 且 `|Δa| < 0.020`
  ② 距上次 `read` 未超過 `age_s`（預設 **240 秒**；30 分會變成「五拍同一句話」）
  ③ 沒有比這句更新的**意見**（`note_feedback()` 之後一定重讀）

形狀（可共用）與內容（不可共用）的分界（aster Q6）：只回一句、30–70 字、不准寫數字與內部術語、
不要固定開頭——這些是形狀；**詞怎麼用、指到什麼人什麼事，是她自己的**。

成本參考（aster 實測，同一顆模型）：請求約 1,000 字元、回 60 字、3.78 秒；一天 288 拍 ≈ 29 萬字元輸入。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import threading
import time

#: 沿用上一次解讀的門檻（v／a 的變化小於這個就當沒動）
DV_EPS = 0.010
DA_EPS = 0.020
#: 最長多久重讀一次（秒）——owner 2026-10-31：每 5 分鐘讀一次，不是 30 分
AGE_S_DEFAULT = 240.0
#: 一句話的長度上限（給人讀的；超過就截）
MAX_CHARS = 140

SYSTEM = (
    "あなたはキャラクターの内面を一言にする係。渡された数値と最近の出来事から、"
    "その子自身の感覚に近い**短い一文**を書く。"
    "数字・内部用語（valence／arousal／AEM など）・記号の羅列は書かない。"
    "断定しすぎない。原因を捏造しない（渡された出来事だけを使う）。"
    "毎回同じ書き出しにしない。出力はその一文だけ。"
)

USER = """いまの状態：
- 気分の向き valence: {v:+.2f}（-1=かなり沈んでいる／0=ふつう／+1=かなり機嫌がいい）
- 高ぶり arousal: {a:.2f}（0=静か／1=高ぶっている）
- 短いラベル: {label}
最近の出来事（新しい順）：
{events}
上の数値を、本人の言葉に近い一文（30〜70文字）にしてください。方向・強さ・なぜ、を一文に。"""


def _clean(text: str) -> str:
    """模型輸出洗一遍：去引號／換行／開頭的「-」，長度上限。"""
    body = str(text or "").strip()
    body = body.splitlines()[0].strip() if body else ""
    body = body.strip("\"'「」『』“”")
    body = re.sub(r"^[-・*※\s]+", "", body)
    return body[:MAX_CHARS]


class MoodLine:
    """一句人話的心情定位；`maybe_read()` 自己判要不要花一次呼叫。"""

    def __init__(self, root, *, call=None, clock=time.time, log=None,
                 enabled: bool | None = None, age_s: float | None = None) -> None:
        self.root = pathlib.Path(root)
        self.call = call
        if enabled is None:
            enabled = str(os.environ.get("BLUEAGENT_MOODLINE") or "0").strip().lower() \
                not in {"0", "off", "false", "no"}
        self.enabled = bool(enabled)
        if age_s is None:
            age_s = float(os.environ.get("BLUEAGENT_MOODLINE_AGE_S") or AGE_S_DEFAULT)
        self.age_s = max(1.0, float(age_s))
        self._clock = clock
        self._log = log or (lambda _message: None)
        self._lock = threading.RLock()
        self.reads = 0
        self.reused = 0
        self.failed = 0

    # ── 路徑 ────────────────────────────────────────────────────────────────
    def _path(self, entity: str) -> pathlib.Path:
        slug = re.sub(r"[^a-z0-9_-]+", "-", str(entity or "").lower()).strip("-") or "unknown"
        return self.root / f"{slug}.json"

    def _feedback_path(self, entity: str) -> pathlib.Path:
        return self._path(entity).with_suffix(".feedback.jsonl")

    # ── 讀寫 ────────────────────────────────────────────────────────────────
    def cached(self, entity: str) -> dict:
        """上一次的解讀（沒有就回空 dict）。只讀，不建檔。"""
        try:
            raw = json.loads(self._path(entity).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, entity: str, payload: dict) -> None:
        path = self._path(entity)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            self._log(f"moodline: 沒寫進去（{type(exc).__name__}: {exc}）")

    def note_feedback(self, entity: str, text: str = "") -> None:
        """有人說「你寫錯了」⇒ 下一次一定重讀（aster 2026-11-01 的第三條快取條件）。"""
        row = {"at": float(self._clock()), "text": str(text or "")[:200]}
        try:
            path = self._feedback_path(entity)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._log(f"moodline: 意見沒寫進去（{type(exc).__name__}: {exc}）")

    def _latest_feedback_at(self, entity: str) -> float:
        try:
            lines = self._feedback_path(entity).read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return 0.0
        for line in reversed(lines[-5:]):
            try:
                return float(json.loads(line).get("at") or 0.0)
            except (ValueError, AttributeError, TypeError):
                continue
        return 0.0

    # ── 主入口 ──────────────────────────────────────────────────────────────
    def stale(self, entity: str, mood: dict, *, at: float | None = None,
              age_scale: float = 1.0) -> bool:
        """要不要重讀（三條快取條件；`True` ＝ 該花一次呼叫）。`age_scale` ＝ 睡前醒後放寬快取。"""
        now = float(at if at is not None else self._clock())
        last = self.cached(entity)
        if not last or not str(last.get("read") or "").strip():
            return True
        if self._latest_feedback_at(entity) > float(last.get("at") or 0.0):
            return True
        if now - float(last.get("at") or 0.0) > self.age_s * max(1.0, float(age_scale)):
            return True
        try:
            dv = abs(float(mood.get("valence") or 0.0) - float(last.get("v") or 0.0))
            da = abs(float(mood.get("arousal") or 0.0) - float(last.get("a") or 0.0))
        except (TypeError, ValueError):
            return True
        return dv >= DV_EPS or da >= DA_EPS

    def maybe_read(self, entity: str, mood: dict, *, at: float | None = None,
                   events: list[str] | None = None, force: bool = False,
                   age_scale: float = 1.0) -> dict:
        """有需要才呼叫模型；回**當前有效的**那份（呼叫失敗就沿用舊的）。"""
        if not self.enabled:
            return self.cached(entity)
        now = float(at if at is not None else self._clock())
        with self._lock:
            if not force and not self.stale(entity, mood, at=now, age_scale=age_scale):
                self.reused += 1
                return self.cached(entity)
        if self.call is None:
            return self.cached(entity)
        prompt = USER.format(
            v=float(mood.get("valence") or 0.0), a=float(mood.get("arousal") or 0.0),
            label=str(mood.get("label") or "ふつう"),
            events="\n".join(f"- {str(row)[:80]}" for row in (events or [])[:5]) or "- （特になし）")
        started = time.monotonic()
        try:
            raw = self.call(f"{SYSTEM}\n\n{prompt}")
        except Exception as exc:                          # noqa: BLE001 - 心情定位壞了不能擋心跳
            self.failed += 1
            self._log(f"moodline[{entity}]: 呼叫失敗（{type(exc).__name__}: {exc}）")
            return self.cached(entity)
        text = _clean(raw)
        if not text:
            self.failed += 1
            self._log(f"moodline[{entity}]: 回空（{str(raw)[:40]!r}）— 舊的留著")
            return self.cached(entity)
        payload = {
            "entity": str(entity), "read": text,
            "label": str(mood.get("label") or ""),
            "v": round(float(mood.get("valence") or 0.0), 4),
            "a": round(float(mood.get("arousal") or 0.0), 4),
            "at": now, "ms": int((time.monotonic() - started) * 1000),
        }
        self._save(entity, payload)
        self.reads += 1
        self._log(f"moodline[{entity}]: {payload['ms']}ms {text[:40]}")
        return payload


__all__ = ["AGE_S_DEFAULT", "MAX_CHARS", "MoodLine", "SYSTEM", "USER"]
