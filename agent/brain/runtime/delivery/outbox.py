"""回合交付的「出匣」（outbox）：**斷線不該讓她的回答消失**。

問題（契約 v2 的洞）：
  · 一輪的回覆是走 `POST /api/turn` 的 SSE 回流出去的；
  · App 半路斷線（地鐵、切換網路、鎖屏）⇒ 之後的 `delta` / `speech` / `done` 全部沒收到；
  · `/api/events` 的 `Last-Event-ID` 只補「主動推送」那條，**補不回這一輪的回答**。

做法：
  · 每輪結束（不論成功或失敗）都落一筆紀錄進 `data/outbox/index.jsonl`（有界：保留最近 N 輪）；
  · 有聲音的回合，把音檔合併成 `data/outbox/<turn_id>.wav`（有界：超過預算就從最舊的開始刪）；
  · App 重連後可以 `GET /api/turns?since=<turn_id>` 問「我漏了哪些」，再逐筆取回文字／音檔；
  · **只看得到自己的**：查詢時拿 principal 過濾（ACL 不能在這裡破）。

紀律：
  · 這裡只存「她說出去的話」，不存對方的文字（那是 transcript 的事）；
  · 音檔有上限，文字沒有（幾百字很便宜）。
"""

from __future__ import annotations

import base64
import io
import json
import os
import pathlib
import threading
import time
import wave

DEFAULT_KEEP = 400
#: 音檔總預算（超過就從最舊的回合開始刪音檔，文字紀錄留著）
DEFAULT_AUDIO_BYTES = 96 * 1024 * 1024


def new_turn_id() -> str:
    """`t<epoch_ms>-<counter>`：單調、可排序、不用額外套件。"""
    with _COUNTER_LOCK:
        global _COUNTER
        _COUNTER += 1
        return f"t{int(time.time() * 1000)}-{_COUNTER:04d}"


_COUNTER = 0
_COUNTER_LOCK = threading.Lock()


def merge_wavs(clips: list[bytes]) -> bytes:
    """把多段 wav 併成一段（同一取樣率／聲道；解不開的就跳過）。"""
    frames: list[bytes] = []
    params = None
    for clip in clips:
        if not clip:
            continue
        try:
            with wave.open(io.BytesIO(clip)) as handle:
                if params is None:
                    params = (handle.getnchannels(), handle.getsampwidth(),
                              handle.getframerate())
                elif params != (handle.getnchannels(), handle.getsampwidth(),
                                handle.getframerate()):
                    continue
                frames.append(handle.readframes(handle.getnframes()))
        except (wave.Error, EOFError):
            continue
    if not frames or params is None:
        return b""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(params[0])
        out.setsampwidth(params[1])
        out.setframerate(params[2])
        for chunk in frames:
            out.writeframes(chunk)
    return buffer.getvalue()


class TurnOutbox:
    """最近 N 輪的交付紀錄（文字一定留、音檔按預算留）。"""

    def __init__(self, root, *, keep: int = DEFAULT_KEEP,
                 audio_bytes: int = DEFAULT_AUDIO_BYTES, clock=time.time) -> None:
        self.root = pathlib.Path(root)
        self.keep = max(10, int(keep))
        self.audio_bytes = max(0, int(audio_bytes))
        self._clock = clock
        self._lock = threading.RLock()
        self.recorded = 0
        self.audio_saved = 0
        self.pruned = 0

    # ── 路徑 ────────────────────────────────────────────────────────────────
    @property
    def index_path(self) -> pathlib.Path:
        return self.root / "index.jsonl"

    def audio_path(self, turn_id: str) -> pathlib.Path:
        safe = "".join(ch for ch in str(turn_id) if ch.isalnum() or ch in "-_")[:64]
        return self.root / f"{safe}.wav"

    # ── 分句「發出即落檔」（owner 2026-11-04）────────────────────────────────
    # 為什麼：以前整輪的紀錄只在**收尾**才寫（而且卡在語音合成之後）⇒ 半路斷線／逾時／
    # 程序被重啟，她已經說出口的那幾句一句都不留。現在 `part` 幀一發出去就先落這一條，
    # 收尾時再匯總成正式的那一列（不等語音、不看有沒有聽完）。
    def parts_path(self, turn_id: str) -> pathlib.Path:
        safe = "".join(ch for ch in str(turn_id) if ch.isalnum() or ch in "-_")[:64]
        return self.root / "parts" / f"{safe}.jsonl"

    def note_part(self, turn_id: str, text: str, *, meta: dict | None = None) -> None:
        """把剛發出去的一句先落檔（失敗不影響交付）。"""
        text = str(text or "").strip()
        if not text:
            return
        path = self.parts_path(turn_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            first = not path.exists()
            with path.open("a", encoding="utf-8") as handle:
                if first and meta:
                    handle.write(json.dumps({"meta": dict(meta)}, ensure_ascii=False) + "\n")
                handle.write(json.dumps({"text": text, "at": round(self._clock(), 3)},
                                        ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass

    def _read_parts(self, turn_id: str) -> tuple[dict, list[str]]:
        try:
            text = self.parts_path(turn_id).read_text(encoding="utf-8")
        except OSError:
            return {}, []
        meta: dict = {}
        parts: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row.get("meta"), dict):
                meta = dict(row["meta"])
            elif str(row.get("text") or "").strip():
                parts.append(str(row["text"]).strip())
        return meta, parts

    def take_parts(self, turn_id: str) -> tuple[dict, list[str]]:
        """讀走分句檔（收尾匯總用），讀完刪掉。"""
        meta, parts = self._read_parts(turn_id)
        try:
            self.parts_path(turn_id).unlink()
        except OSError:
            pass
        return meta, parts

    def recover_pending(self) -> int:
        """開機對帳：orphan 分句補新列；逾時後才到的分句併回已存在的那一輪。

        逾時收尾會先落一筆正式列，但背景 job 之後仍可能發出 `part`。這些遲到分句
        不是垃圾，也不能因為正式列已存在就刪掉；要接在原本文字後面，狀態與 ACL 照舊。
        """
        root = self.root / "parts"
        fixed = 0
        try:
            files = sorted(root.glob("*.jsonl"))
        except OSError:
            return 0
        for path in files:
            turn_id = path.stem
            meta, parts = self._read_parts(turn_id)
            if not parts:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            existing = self.get(turn_id)
            if existing is not None:
                previous = [str(item) for item in (existing.get("parts") or [])]
                if not previous and str(existing.get("text") or ""):
                    previous = [str(existing["text"])]
                # 若上一次對帳已追加過、只是刪檔前重啟，不能再把同一段接一次。
                additions = [] if previous[-len(parts):] == parts else list(parts)
                if additions:
                    merged = dict(existing)
                    merged["text"] = str(existing.get("text") or "") + "".join(additions)
                    combined = previous + additions
                    if len(combined) > 1:
                        merged["parts"] = combined
                    self._append_row(merged)
                    fixed += 1
            else:
                try:
                    self.record(turn_id=turn_id,
                                principal=str(meta.get("principal") or ""),
                                entity=str(meta.get("entity") or ""),
                                text="".join(parts), parts=parts, ok=False,
                                error="incomplete", asked=str(meta.get("asked") or ""),
                                source=str(meta.get("source") or ""))
                    fixed += 1
                except Exception:                    # noqa: BLE001 - 對帳失敗不該擋開機
                    continue
            try:
                path.unlink()
            except OSError:
                pass
        return fixed

    def _append_row(self, row: dict) -> dict:
        """把對帳後的完整舊列新版本追加到索引（音檔不重寫、欄位不遺失）。"""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with self.index_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                self.recorded += 1
                self._prune_locked()
        except OSError:
            pass
        return row

    # ── 寫 ──────────────────────────────────────────────────────────────────
    def record(self, *, turn_id: str, principal: str, entity: str, text: str,
               thread: str = "", ok: bool = True, error: str = "", audio: bytes = b"",
               modes: dict | None = None, frames: int = 0,
               speech: int = 0, asked: str = "", source: str = "",
               asked_sticker: str = "", stickers: list | None = None,
               parts: list | None = None, asked_parts: list | None = None,
               photos: list | None = None,
               kind: str = "", call_s: int = 0) -> dict:
        row = {"turn_id": str(turn_id), "at": round(self._clock(), 3),
               "principal": str(principal or ""), "entity": str(entity or ""),
               "thread": str(thread or ""), "text": str(text or ""),
               # `source`＝這一輪從哪個端進來（`phone:owner`／`discord:owner`／`discord:dm:owner`／`discord:vc`／`trpg:…`）。
               # App 的「對話紀錄」只認 `phone:`（owner 2026-10-27：app 與 Discord 的紀錄各自獨立）。
               "source": str(source or ""),
               # `asked`＝使用者那一句（歷史要兩側都畫；舊資料沒有這個鍵 ⇒ 客戶端當空字串）
               "asked": str(asked or ""),
               "ok": bool(ok), "error": str(error or ""),
               "frames": int(frames or 0), "speech": int(speech or 0),
               "audio": False, "bytes": 0}
        # 表情包（owner 2026-10-29）：歷史要畫圖、不畫「（スタンプ：…）」那串字
        #   `asked_sticker`＝使用者送的那張；`stickers`＝她這一輪回的（`{id, entity, url}`）
        if asked_sticker:
            row["asked_sticker"] = str(asked_sticker)
        if stickers:
            row["stickers"] = [dict(item) for item in stickers if isinstance(item, dict)]
        # `photos`＝她這一輪發出來的**圖**（委任出圖）：App 靠 url 重畫，Discord 靠 id 取檔。
        if photos:
            row["photos"] = [dict(item) for item in photos if isinstance(item, dict) and item.get("url")]
        # `asked_parts`＝他自己發的那幾條（延時聚合的一輪可能有多筆：文字／照片／表情包）。
        # 客戶端歷史要跟當時一樣畫成好幾條，而不是合成一條（與 `parts` 對稱，owner 2026-11-04）。
        if asked_parts:
            rows = [dict(item) for item in asked_parts if isinstance(item, dict)]
            # ⚠️ 只有「一條純文字」才不必存（等同舊的 `asked`）；單張貼圖／單張照片**必須**存，
            # 否則歷史畫不出他那條（延時聚合之後那條路不再走 legacy 的 `asked_sticker`）。
            if len(rows) > 1 or any(str(item.get("kind") or "text") != "text" for item in rows):
                row["asked_parts"] = rows
        # `parts`＝這一輪回覆的分段（`reply_split`，與 SSE `part` 幀同一份切句）。
        # 只有真的分段（>1 條）才寫：串流時她是「一條一條短訊息」，補拿／歷史要長得一樣
        # （owner 2026-11-04 指出的對齊問題）。舊列沒有這個鍵 ⇒ 客戶端照舊畫成一整條。
        if parts:
            groups = [str(item) for item in parts if str(item).strip()]
            if len(groups) > 1:
                row["parts"] = groups
        # `kind`＝這一列的**用途**（owner 2026-10-29）：
        #   `"call"`＝App 單人通話裡的一輪 ⇒ **不進 App 的對話紀錄**（通話內容不留）；
        #   `"call-log"`＝通話結束時那一條標記（「發生過通話」）。
        #   舊列沒有這個鍵 ⇒ 當一般回合。
        if kind:
            row["kind"] = str(kind)
        # `call_s`＝通話時長（秒）：客戶端拿它按**界面語言**說「通話 3分12秒」。
        #   只寫數字，不把日文句子烤進資料（界面語言有三種）。
        if call_s:
            row["call_s"] = int(call_s)
        if audio:
            path = self.audio_path(turn_id)
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                path.write_bytes(audio)
                row["audio"] = True
                row["bytes"] = len(audio)
                with self._lock:
                    self.audio_saved += 1
            except OSError:
                pass
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with self.index_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                self.recorded += 1
                self._prune_locked()
        except OSError:
            return row
        return row

    def record_delivery(self, principal: str, frame: dict) -> dict:
        """推到 App 的交付（主動訊息／後續交付）也落一筆，回 `{"turn_id": …}` 讓推播帶著走。

        以前這兩種只走匯流排（記憶體）：App 前景看著 A 時 B 說的話被丟掉，切到 B 後
        `/api/turns` 裡也沒有 ⇒ 永遠看不到（unity-review U-4，2026-10-28）。
        沒文字／沒角色的控制幀、已經帶 `turn_id` 的（那一輪自己會落）都不記。
        """
        frame = frame or {}
        text = str(frame.get("text") or "").strip()
        entity = str(frame.get("entity") or "").strip()
        if not text or not entity or frame.get("turn_id"):
            return {}
        turn_id = new_turn_id()
        kind = str(frame.get("kind") or "push")
        # 交付自己帶了來源（後續交付＝原本那一輪從哪個端進來）就照用：Discord 語音頻道交代的工作
        # （`discord:vc`）做完也會推到 App，但不能記成 App 的對話（owner 2026-10-27「App 與 DC 各自獨立」，
        # 複檢 2026-10-28 抓到以前一律寫 `phone:`）。沒帶來源的（主動訊息）才算 App 這一端。
        source = str(frame.get("source") or "").strip() or f"phone:{kind}"
        self.record(turn_id=turn_id, principal=str(principal or ""), entity=entity, text=text,
                    thread=str(frame.get("thread") or ""), source=source)
        return {"turn_id": turn_id}

    def _prune_locked(self) -> None:
        rows = self._read_rows()
        if len(rows) > self.keep:
            drop = rows[:-self.keep]
            kept = rows[-self.keep:]
            for row in drop:
                if row.get("audio"):
                    try:
                        self.audio_path(str(row["turn_id"])).unlink()
                    except OSError:
                        pass
                try:                                   # 分句檔正常已在收尾時收走，殘檔一起清
                    self.parts_path(str(row["turn_id"])).unlink()
                except OSError:
                    pass
            self._write_rows(kept)
            self.pruned += len(drop)
        # 音檔預算：從最舊的開始丟音檔（文字留著）
        if self.audio_bytes <= 0:
            return
        rows = self._read_rows()
        total = sum(int(row.get("bytes") or 0) for row in rows)
        if total <= self.audio_bytes:
            return
        changed = False
        for row in rows:
            if total <= self.audio_bytes:
                break
            if not row.get("audio"):
                continue
            try:
                self.audio_path(str(row["turn_id"])).unlink()
            except OSError:
                pass
            total -= int(row.get("bytes") or 0)
            row["audio"] = False
            row["bytes"] = 0
            changed = True
        if changed:
            self._write_rows(rows)

    def _read_rows(self) -> list[dict]:
        try:
            text = self.index_path.read_text(encoding="utf-8")
        except OSError:
            return []
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def _write_rows(self, rows: list[dict]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        try:
            tmp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                   for row in rows), encoding="utf-8")
            tmp.replace(self.index_path)
        except OSError:
            pass

    # ── 讀 ──────────────────────────────────────────────────────────────────
    def get(self, turn_id: str, *, principal: str | None = None) -> dict | None:
        for row in reversed(self._read_rows()):
            if str(row.get("turn_id")) != str(turn_id):
                continue
            if principal is not None and str(row.get("principal")) != str(principal):
                return None                       # ACL：只看得到自己的
            return dict(row)
        return None

    def since(self, turn_id: str = "", *, principal: str | None = None,
              limit: int = 50) -> list[dict]:
        """比 `turn_id` 之後的紀錄（含「找不到那個 id」⇒ 全部回，讓端末自己對齊）。"""
        rows = self._read_rows()
        if principal is not None:
            rows = [row for row in rows if str(row.get("principal")) == str(principal)]
        if turn_id:
            index = next((pos for pos, row in enumerate(rows)
                          if str(row.get("turn_id")) == str(turn_id)), None)
            if index is not None:
                rows = rows[index + 1:]
            else:
                rows = rows[-max(1, int(limit)):]
        return rows[-max(1, int(limit)):]

    def read_audio(self, turn_id: str, *, principal: str | None = None) -> bytes:
        row = self.get(turn_id, principal=principal)
        if not row or not row.get("audio"):
            return b""
        try:
            return self.audio_path(turn_id).read_bytes()
        except OSError:
            return b""

    def snapshot(self, *, principal: str | None = None) -> dict:
        rows = self._read_rows()
        if principal is not None:
            rows = [row for row in rows if str(row.get("principal")) == str(principal)]
        stats = {"turns": len(rows), "keep": self.keep,
                 "audio_files": sum(1 for row in rows if row.get("audio")),
                 "audio_bytes": sum(int(row.get("bytes") or 0) for row in rows)}
        if principal is None:
            # process 計數是全域資料；按人查詢時不能順手洩給其他 principal。
            stats.update({"recorded": self.recorded, "pruned": self.pruned})
        return stats


__all__ = ["DEFAULT_AUDIO_BYTES", "DEFAULT_KEEP", "TurnOutbox", "merge_wavs", "new_turn_id"]
