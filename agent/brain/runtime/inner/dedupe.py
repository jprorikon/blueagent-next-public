"""跨源同句的合併索引：**30 秒內、非同源、同一句話 ⇒ 算一件**（owner 2026-11-01 定）。

為什麼要有這條：AEM 的 message receipt 靠 `event_id` 去重（`aem_v2.apply_event`，
同一個 id 回 `DUPLICATE`），而我們 `note_message` 預設拿「時鐘＋文本」算 id
⇒ 同一句話從手機與 Discord 各進來一次，就是兩條 receipt、兩次判定、valence／arousal 各加一次。

強度翻倍只是表面，真正問題是**入口分布會把尺帶歪**：手機常斷、Discord 常開，
偏移來自「他從哪個端說話」而不是「他說了什麼」。

邊界（刻意選的）：
  · **同源重複不併**——他真有可能在 30 秒內講兩次同一句。
  · 只做**字串**比對（NFKC＋空白收斂），**不做語意／主題**——AEM 不認識主題，
    這裡只負責回答「兩個入口收到的是不是同一串字」。
  · 窗內紀錄落盤（`<root>/_dedupe/<entity>.json`，0600）：重啟不該漏併。
  · `window_s = 0` ⇒ 整條規則關掉（回舊行為）。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import threading
import time
import unicodedata

#: 跨源同句的合併窗口（秒）
WINDOW_S_DEFAULT = 30.0
#: 索引放這裡（與核心的 store 分開，別踩到 `state.json`）
DIR_NAME = "_dedupe"
#: 每個角色最多留幾筆（窗很短，這個上限只是防檔案長大）
MAX_ROWS = 64


def fingerprint(text: str) -> str:
    """同一句話的指紋：NFKC（全/半形統一）→ 去頭尾 → 空白收斂 → sha1 前 16。"""
    body = unicodedata.normalize("NFKC", str(text or "")).strip()
    body = re.sub(r"\s+", " ", body)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def source_key(source: str, fallback: str = "unknown") -> str:
    """入口名：`discord:dm:xxx` / `phone:xxx` / `mic:…` → `discord` / `phone` / `mic`。"""
    raw = str(source or "").strip()
    if not raw:
        raw = str(fallback or "unknown")
        return re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-") or "unknown"
    return re.sub(r"[^a-z0-9_-]+", "-", raw.split(":", 1)[0].lower()).strip("-") or "unknown"


class CrossSourceDedupe:
    """窗內的「同一句話、從別的入口」查表；`hit()` 只讀、`note()` 才寫。"""

    def __init__(self, root, *, window_s: float | None = None, clock=time.time, log=None) -> None:
        self.root = pathlib.Path(root)
        env = os.environ.get("BLUEAGENT_AEM_DEDUPE_S")
        if window_s is None:
            window_s = WINDOW_S_DEFAULT if env is None else float(env)
        self.window_s = max(0.0, float(window_s))
        self._clock = clock
        self._log = log or (lambda _message: None)
        self._lock = threading.RLock()
        self._rows: dict[str, list[dict]] = {}
        self._loaded: set[str] = set()

    # ── 對外 ────────────────────────────────────────────────────────────────
    def hit(self, entity: str, fp: str, source: str) -> dict | None:
        """窗內有沒有「同一句話、從**別的**入口」進來的紀錄？有就回 `{event_id, source, age}`。"""
        if self.window_s <= 0:
            return None
        now = float(self._clock())
        for row in self._rows_for(entity, now=now):
            if row["fp"] == fp and row["source"] != source:
                return {"event_id": row["event_id"], "source": row["source"],
                        "age": max(0.0, now - float(row["at"]))}
        return None

    def note(self, entity: str, fp: str, source: str, event_id: str) -> None:
        """寫一筆（只在 message 真的 apply 成功之後）；落盤失敗不擋對話。"""
        if self.window_s <= 0:
            return
        now = float(self._clock())
        with self._lock:
            rows = self._rows_for(entity, now=now)
            rows.append({"fp": fp, "at": now, "source": source, "event_id": event_id})
            self._rows[entity] = rows[-MAX_ROWS:]
            payload = list(self._rows[entity])
        self._save(entity, payload)

    # ── 內部 ────────────────────────────────────────────────────────────────
    def _path(self, entity: str) -> pathlib.Path:
        slug = re.sub(r"[^a-z0-9_-]+", "-", str(entity or "").lower()).strip("-") or "unknown"
        return self.root / DIR_NAME / f"{slug}.json"

    def _rows_for(self, entity: str, *, now: float) -> list[dict]:
        """窗內的紀錄（順手丟掉過期的）。第一次碰到這個角色時從檔案接回。"""
        with self._lock:
            if entity not in self._loaded:
                self._loaded.add(entity)
                self._rows[entity] = self._read(entity)
            rows = [row for row in self._rows.get(entity, [])
                    if self.window_s > 0 and 0.0 <= now - float(row["at"]) <= self.window_s]
            self._rows[entity] = rows
            return rows

    def _read(self, entity: str) -> list[dict]:
        try:
            raw = json.loads(self._path(entity).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return []
        rows: list[dict] = []
        for row in raw if isinstance(raw, list) else []:
            if isinstance(row, dict) and isinstance(row.get("fp"), str) \
                    and isinstance(row.get("event_id"), str):
                rows.append({"fp": row["fp"], "at": float(row.get("at") or 0.0),
                             "source": str(row.get("source") or "unknown"),
                             "event_id": row["event_id"]})
        return rows

    def _save(self, entity: str, payload: list[dict]) -> None:
        path = self._path(entity)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            self._log(f"affect: 合併索引沒寫進去（{type(exc).__name__}: {exc}）")


__all__ = ["WINDOW_S_DEFAULT", "CrossSourceDedupe", "fingerprint", "source_key"]
