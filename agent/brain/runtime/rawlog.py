"""Raw 原文倉：**一個角色一個倉**，append-only，保留 12 個月（owner 2026-10-23）。

為什麼要有這一層：`threads.json` 是**對話狀態**（有上限、要快速存取），滾動窗一過就沒有原文了。
上一輪就是這樣丟掉「主人在讀《佐々木とピーちゃん》」——事實沒抽到、原文也早被 segment 上限擠掉。

佈局：

    data/raw/<entity>/<YYYY-MM>.jsonl      # 一行一輪，只追加
    data/raw/<entity>/.watermark.json      # thread_id → 最後落過的 turn_id（重啟不重複寫）

設計約束：
  · domain 不碰 I/O ⇒ 由 `ThreadStore.save()` 之後呼叫 `sync_thread()`。
  · 只追加、不改寫；檢索用掃描（P1），索引留到 P3 跟 `memory.search` 一起做。
  · `room` 記**寫入當下的房間成員**（含她自己）——披露層判可見性要用它，不能只看現在的房間。
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from calendar import monthrange
from typing import Iterable

from agent.brain.memory.facts import grams

MONTH_FMT = "%Y-%m"
DEFAULT_KEEP_MONTHS = 12
#: 水準檔裡「這一輪最後一條的 at」的鍵尾（`<thread_id>@at`）。舊版只有 turn_id，
#: 沒這個欄位時退回舊行為（並在對不上時改成「比對既有 raw」而不是放棄）。
AT_SUFFIX = "@at"


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_at(value) -> str:
    return f"{float(value or 0.0):.6f}"


def _turn_key(thread_id: str, turn) -> tuple[str, str, float]:
    """(thread, turn, at)：**同一輪有兩條記錄**（user／assistant 共用 turn_id）⇒ at 要進 key。"""
    return (str(thread_id), str(getattr(turn, "turn_id", "")),
            round(float(getattr(turn, "at", 0.0) or 0.0), 6))

#: 每個 entity 目錄一把**模組級**鎖：水印是檔案，兩個 RawLog 實例（或同一實例的兩個
#: 執行緒）同時「讀水印→追加→寫水印」會互相蓋掉對方的新水印，於是下一輪又把整段
#: 重寫一次。2026-10-24 實測後果：9369 行只有 157 個不同的 turn，一條被寫了 169 次。
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _month_start(now: float) -> tuple[int, int]:
    stamp = time.localtime(float(now))
    return stamp.tm_year, stamp.tm_mon


def _shift_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


class RawLog:
    def __init__(self, root, *, keep_months: int = DEFAULT_KEEP_MONTHS,
                 clock=time.time) -> None:
        self.root = pathlib.Path(root)
        self.keep_months = max(1, int(keep_months))
        self._clock = clock
        self._lock = threading.RLock()
        #: entity → {(thread_id, turn_id, at)}：本程序寫過的（避免水印失效時重複 append）
        self._written: dict[str, set[tuple[str, str, float]]] = {}

    # ── 路徑 ────────────────────────────────────────────────────────────────
    def entity_dir(self, entity: str) -> pathlib.Path:
        return self.root / str(entity or "unknown")

    def month_path(self, entity: str, at: float) -> pathlib.Path:
        year, month = _month_start(at)
        return self.entity_dir(entity) / f"{year:04d}-{month:02d}.jsonl"

    def _watermark_path(self, entity: str) -> pathlib.Path:
        return self.entity_dir(entity) / ".watermark.json"

    # ── 寫 ──────────────────────────────────────────────────────────────────
    def _load_watermark(self, entity: str) -> dict[str, str]:
        path = self._watermark_path(entity)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError):
            return {}

    def _save_watermark(self, entity: str, marks: dict[str, str]) -> None:
        path = self._watermark_path(entity)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(marks, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def sync_thread(self, thread, *, now: float | None = None) -> int:
        """把 thread 裡「還沒落過」的輪次追加進 raw。回本次寫入條數。

        ⚠️ **2026-11-01 修正（nori 那次「只記得要出門、不知道去哪」的根因）**：
        以前水準只記 `turn_id`，而**一輪有兩條記錄**（`user` 與 `assistant` 共用同一個
        `turn_id`、`at` 差幾秒）⇒ `ids.index(last) + 1` 找到的是該 id 的**第一條**，
        切片後把同一輪的 assistant 條**又寫一遍**（raw 裡「同一輪寫 9 次」就是這樣來的）。
        更糟的是 segment 一旦被封存搬走，水準那個 id 不在 segment 裡 ⇒ 這支直接
        `turns = []` **整條線永久停寫**（nori 的 11:29–11:45 那九輪就是這樣丟的，
        裡面有「池袋あたりに電器店まわりでもしようかと」）。
        現在水準多存一個 `@at`（這一輪最後一條的 `at`）：按「時間」切片，
        既不會重寫同一輪的另一條，segment 換過也不會放棄——真的對不上時退回
        「比對既有 raw 檔的 (thread, turn, at)」再寫，寧可多掃一次檔，不要靜默丟資料。
        """
        key = getattr(thread, "key", None)
        entity = str(getattr(key, "entity", "") or getattr(thread, "entity", "") or "")
        if not entity:
            return 0
        with self._lock, _lock_for(str(self.entity_dir(entity))):
            marks = self._load_watermark(entity)
            thread_id = str(getattr(thread, "id", "") or "")
            last = marks.get(thread_id, "")
            last_at = _as_float(marks.get(thread_id + AT_SUFFIX))
            turns, stale = self._pending(list(getattr(thread, "segment", ()) or ()), last, last_at)
            if not turns:
                return 0
            if stale:
                # 水準那一輪已被 segment 搬走（舊版水準沒有 @at）：退回「比對既有 raw」再寫，
                # 寧可多掃一次檔，也不要靜默丟資料（2026-11-01 nori 那九輪就是這樣丟的）
                turns = self._drop_known(entity, thread_id, turns)
                if not turns:
                    return 0
            # 本程序這一輪已經寫過的不再寫（水印被別人蓋掉時的第二道保險）
            seen = self._written.setdefault(entity, set())
            fresh = [turn for turn in turns if _turn_key(thread_id, turn) not in seen]
            if not fresh:
                return 0
            turns = fresh
            self.entity_dir(entity).mkdir(parents=True, exist_ok=True)
            for turn in turns:
                row = self._row(entity, thread, turn, now=now)
                path = self.month_path(entity, row["at"])
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            for turn in turns:
                seen.add(_turn_key(thread_id, turn))
            # 寫回前先重讀一次再合併：別的執行緒／實例剛寫進去的水印不能被我們蓋掉
            marks = {**self._load_watermark(entity),
                     thread_id: str(getattr(turns[-1], "turn_id", "")),
                     thread_id + AT_SUFFIX: _fmt_at(getattr(turns[-1], "at", 0.0))}
            self._save_watermark(entity, marks)
            return len(turns)

    # ── 水準與切片 ──────────────────────────────────────────────────────────
    def _pending(self, turns: list, last: str, last_at: float | None) -> tuple[list, bool]:
        """segment 裡「水準之後」的那些輪（含同輪的第二條記錄）。

        回 `(待寫的輪, 水準是否已失效)`——失效時呼叫端要再跟既有 raw 比對一次。
        """
        if not turns:
            return [], False
        if last_at is not None:
            later = [turn for turn in turns
                     if float(getattr(turn, "at", 0.0) or 0.0) > last_at + 1e-6]
            return later, False                  # 有 @at 就不會失效（只是可能沒有新的）
        if last:
            found = None
            for index, turn in enumerate(turns):       # ★ 取**最後一條**同 id 的記錄
                if str(getattr(turn, "turn_id", "")) == last:
                    found = index
            if found is not None:
                return turns[found + 1:], False
            return turns, True                   # 水準被搬走了：交給呼叫端比對既有 raw
        return turns, False

    def _drop_known(self, entity: str, thread_id: str, turns: list) -> list:
        """把「raw 檔裡已經有的」剔掉（只掃這幾輪跨到的月份檔）。"""
        known: set[tuple[str, float]] = set()
        months = {self.month_path(entity, float(getattr(turn, "at", 0.0) or 0.0)) for turn in turns}
        for path in months:
            try:
                handle = path.open("r", encoding="utf-8")
            except OSError:
                continue
            with handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if str(row.get("thread") or "") != thread_id:
                        continue
                    known.add((str(row.get("turn") or ""), round(float(row.get("at") or 0.0), 6)))
        return [turn for turn in turns
                if (str(getattr(turn, "turn_id", "")),
                    round(float(getattr(turn, "at", 0.0) or 0.0), 6)) not in known]


    def _row(self, entity: str, thread, turn, *, now=None) -> dict:
        key = getattr(thread, "key", None)
        at = float(getattr(turn, "at", 0.0) or (now if now is not None else self._clock()))
        participants = {str(who).strip() for who in (getattr(thread, "participants", ()) or ())}
        room = sorted(participants | {entity})
        return {
            "at": at,
            "entity": entity,
            "turn": str(getattr(turn, "turn_id", "")),
            "thread": str(getattr(thread, "id", "")),
            "scene": str(getattr(key, "scene", "")),
            # 這條線的用途（chat／trpg…；DIAGNOSIS 2026-11-10 B1）：以前只抄了 source，
            # 遊戲那幾桌的 scene 又被寫成 together ⇒ 讀的一側分不出這是不是遊戲。舊行沒有這欄。
            "purpose": str(getattr(key, "purpose", "")),
            "role": str(getattr(turn, "role", "")),
            "speaker": str(getattr(turn, "speaker", "")),
            "addressed_to": str(getattr(turn, "addressed_to", "")),
            "source": str(getattr(turn, "source", "")),
            "room": room,
            "text": str(getattr(turn, "text", "") or ""),
            "audio": "",
            "engage": str(getattr(turn, "engage", "")),
            "generation": int(getattr(key, "generation", 0) or 0)
            if hasattr(key, "generation") else 0,
        }

    # ── 保留期 ──────────────────────────────────────────────────────────────
    def rotate(self, entity: str | None = None, *, now: float | None = None) -> list[str]:
        """刪掉超過 `keep_months` 的月檔（預設 12 個月）。回被刪掉的路徑。"""
        now = self._clock() if now is None else float(now)
        year, month = _month_start(now)
        cutoff_year, cutoff_month = _shift_months(year, month, -self.keep_months)
        removed: list[str] = []
        roots = [self.entity_dir(entity)] if entity else [
            path for path in self.root.glob("*") if path.is_dir()]
        for folder in roots:
            for path in sorted(folder.glob("*.jsonl")):
                try:
                    stamp_year, stamp_month = (int(part) for part in path.stem.split("-"))
                except ValueError:
                    continue
                if (stamp_year, stamp_month) < (cutoff_year, cutoff_month):
                    path.unlink(missing_ok=True)
                    removed.append(str(path))
        return removed

    # ── 讀 ──────────────────────────────────────────────────────────────────
    def _iter_rows(self, entity: str, *, since: float | None = None,
                   until: float | None = None) -> Iterable[dict]:
        """逐行吐原文；**同一個 (thread, turn) 只吐第一次**。

        舊資料裡有重複 append（2026-10-24：9369 行只有 157 個 turn），重複行會讓
        同一句話在檢索／提煉裡被算很多次。這裡去重，舊檔不必先清也能讀對。
        """
        seen: set[tuple[str, str]] = set()
        for path in sorted(self.entity_dir(entity).glob("*.jsonl")):
            try:
                handle = path.open("r", encoding="utf-8")
            except OSError:
                continue
            with handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    at = float(row.get("at") or 0.0)
                    if since is not None and at < float(since):
                        continue
                    if until is not None and at > float(until):
                        continue
                    key = (str(row.get("thread") or ""), str(row.get("turn") or ""))
                    if key[1]:
                        if key in seen:
                            continue
                        seen.add(key)
                    yield row

    def search(self, entity: str, query: str, *, limit: int = 5,
               since: float | None = None, until: float | None = None,
               room: Iterable[str] | None = None) -> list[dict]:
        """按「二字重疊」找原文；`room`（＝現在在場的人）給了就只回 **`room ⊆ 那次房間成員`** 的輪次。"""
        wanted = grams(query)
        if not wanted:
            return []
        allow = None if room is None else {str(who).strip() for who in room}
        scored: list[tuple[float, dict]] = []
        for row in self._iter_rows(entity, since=since, until=until):
            text = str(row.get("text") or "")
            if allow is not None and not (row.get("room") and allow <= set(row.get("room"))):
                continue
            overlap = len(wanted & grams(text)) / max(1, len(wanted))
            if overlap > 0:
                scored.append((overlap, row))
        scored.sort(key=lambda item: (item[0], float(item[1].get("at") or 0.0)), reverse=True)
        return [row for _, row in scored[:limit]]

    def recent(self, entity: str, *, limit: int = 12) -> list[dict]:
        """最近幾輪原文（給「提前想」用）。"""
        rows = list(self._iter_rows(entity))
        rows.sort(key=lambda row: float(row.get("at") or 0))
        return rows[-max(1, int(limit)):]

    def stats(self, entity: str) -> dict:
        rows = list(self._iter_rows(entity))
        months = sorted(path.stem for path in self.entity_dir(entity).glob("*.jsonl"))
        return {"entity": entity, "turns": len(rows), "months": months,
                "keep_months": self.keep_months}
