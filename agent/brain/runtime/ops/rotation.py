"""日誌／資料輪轉：**append-only 的檔案要有上限**（owner 2026-10-22 指定的第二件事）。

三種東西會無限長大，處理方式不一樣：

| 檔案 | 生長方式 | 處理 |
|---|---|---|
| `logs/turns.jsonl` | 一輪一行 | **輪轉** `turns.jsonl.1 … .N`，最舊的丟掉 |
| `logs/activity-<entity>.jsonl` | 一則活動一行 | 同上（每角色一份） |
| `attention.jsonl` / `episodes.jsonl` | 一行一筆 | 同上 |
| `threads.json` | 一個 JSON 物件，**不能切半** | 不是輪轉而是「上限＋封存」：`transcript`／`segment` 只留在記憶體最近 N 條，超出的 append 進 `data/archive/threads-<YYYYMM>.jsonl`，載入時若檔案過大先做一次封存 |

刻意不做的事：
  · 不用 logrotate / 外部 cron：容器裡沒有那些東西，且輪轉要和寫入同一把鎖。
  · 不壓縮（`.gz`）預設關閉——現階段資料量小，壓縮只讓「出事時 grep」變麻煩。
  · **不刪 archive**：封存是「搬走」，不是「消滅」；要清理由人決定。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time

#: 單檔上限（5 MB ≈ 三～五萬行 turn 紀錄）。
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
#: 留幾個世代（`.1` 最新）。
DEFAULT_KEEP = 5
#: 每寫這麼多行才 stat 一次檔案大小（避免每輪都 stat）。
DEFAULT_EVERY = 64

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: pathlib.Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def max_bytes_from_env(default: int = DEFAULT_MAX_BYTES) -> int:
    """`BLUEAGENT_LOG_MAX_BYTES`（0 ＝ 關掉輪轉，給測試或除錯用）。"""
    try:
        return max(0, int(os.environ.get("BLUEAGENT_LOG_MAX_BYTES") or default))
    except (TypeError, ValueError):
        return default


def keep_from_env(default: int = DEFAULT_KEEP) -> int:
    try:
        return max(1, int(os.environ.get("BLUEAGENT_LOG_KEEP") or default))
    except (TypeError, ValueError):
        return default


def rotate_if_needed(path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                     keep: int = DEFAULT_KEEP) -> bool:
    """檔案超過 `max_bytes` ⇒ `path.1 … path.N` 輪轉一次。回「這次有沒有轉」。

    原子性：`os.replace` 逐級搬，最後把原檔清空（**不刪檔**，因為呼叫端還握著它的寫入路徑）。
    轉完之後 `path` 是 0 byte 的新檔——正在寫的那支檔案 handle 若已開著，會繼續寫到舊 inode；
    所以呼叫端必須「寫完一行、關檔」再呼叫（本專案所有 JSONL 寫入都是這種形狀）。
    """
    if max_bytes <= 0:
        return False
    path = pathlib.Path(path)
    with _lock_for(path):
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size <= max_bytes:
            return False
        keep = max(1, int(keep))
        try:
            oldest = path.with_name(f"{path.name}.{keep}")
            if oldest.exists():
                oldest.unlink()
            for index in range(keep - 1, 0, -1):
                source = path.with_name(f"{path.name}.{index}")
                if source.exists():
                    os.replace(source, path.with_name(f"{path.name}.{index + 1}"))
            os.replace(path, path.with_name(f"{path.name}.1"))
        except OSError:
            return False
        try:
            path.touch()
        except OSError:
            return False
        return True


class RotationGuard:
    """掛在「每次寫一行」的呼叫端：每 `every` 次才真的去看檔案大小。"""

    def __init__(self, path, *, max_bytes: int | None = None, keep: int | None = None,
                 every: int = DEFAULT_EVERY) -> None:
        self.path = pathlib.Path(path)
        self.max_bytes = max_bytes_from_env() if max_bytes is None else int(max_bytes)
        self.keep = keep_from_env() if keep is None else int(keep)
        self.every = max(1, int(every))
        self.rotations = 0
        self._count = 0
        self._lock = threading.Lock()

    def note_write(self) -> bool:
        """寫完一行之後叫一次；回「這次剛好輪轉了」。"""
        with self._lock:
            self._count += 1
            due = self.max_bytes > 0 and (self._count % self.every == 0)
        if not due:
            return False
        return self.rotate_now()

    def rotate_now(self) -> bool:
        rotated = rotate_if_needed(self.path, max_bytes=self.max_bytes, keep=self.keep)
        if rotated:
            with self._lock:
                self.rotations += 1
        return rotated


# ── 目錄用量（`/api/metrics` 的 data_bytes）──────────────────────────────────
def dir_bytes(path) -> int:
    """目錄（或單檔）的實際位元組數；不存在＝0。"""
    root = pathlib.Path(path)
    if root.is_file():
        try:
            return root.stat().st_size
        except OSError:
            return 0
    total = 0
    if not root.is_dir():
        return 0
    for child in root.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def usage(root) -> dict:
    """`data/` 下每個項目的大小（人看的單位也給一份）。"""
    base = pathlib.Path(root)
    out: dict[str, dict] = {}
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir()):
        if child.name in {"__pycache__"}:
            continue
        size = dir_bytes(child)
        out[child.name] = {"bytes": size, "mb": round(size / 1024 / 1024, 2)}
    return out


# ── 封存（threads.json 用）────────────────────────────────────────────────────
def month_key(at: float | None = None) -> str:
    return time.strftime("%Y%m", time.localtime(at if at is not None else time.time()))


def append_archive(directory, prefix: str, rows: list[dict], *,
                   month: str | None = None) -> pathlib.Path | None:
    """把一批列 append 進 `<prefix>-<YYYYMM>.jsonl`；回檔案路徑（沒有列＝None）。"""
    if not rows:
        return None
    folder = pathlib.Path(directory)
    target = folder / f"{prefix}-{month or month_key()}.jsonl"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return target


# ── 分級輪轉：按天切 ＋ 保留期 ────────────────────────────────────────────────
# owner 2026-10-22：「attention／episodes 輪轉分級（按天切／保留期）」。
#
# 為什麼不能只用「大小輪轉」：大小輪轉只知道「裝滿了」，不知道「多久以前」。
# 事故排查要的是「昨天那段」、記憶回顧要的是「上個月那幾段」——按天切才對得上人腦；
# 保留期才擋得住「一年後資料庫長成 40 GB」。
#
# 分級（預設，可用 env 改）：
#   attention（她在注意什麼，量大、只為排查）  14 天
#   activity（誰做了什麼，中等）              30 天
#   episodes（收片摘要，屬於記憶的旁證）        90 天
#   turns（每輪結構化紀錄）                    30 天

DEFAULT_ATTENTION_KEEP_DAYS = 14
DEFAULT_ACTIVITY_KEEP_DAYS = 30
DEFAULT_EPISODES_KEEP_DAYS = 90
DEFAULT_TURNS_KEEP_DAYS = 30


def day_key(at: float | None = None) -> str:
    """`YYYYMMDD`（本地時區：保留期要跟人看到的日曆一致）。"""
    return time.strftime("%Y%m%d", time.localtime(at if at is not None else time.time()))


def _days_from_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


def daily_keep_days(kind: str) -> int:
    """`kind` ＝ `attention` / `activity` / `episodes` / `turns`。0 ＝ 不按天切。"""
    table = {"attention": ("BLUEAGENT_ATTENTION_KEEP_DAYS", DEFAULT_ATTENTION_KEEP_DAYS),
             "activity": ("BLUEAGENT_ACTIVITY_KEEP_DAYS", DEFAULT_ACTIVITY_KEEP_DAYS),
             "episodes": ("BLUEAGENT_EPISODES_KEEP_DAYS", DEFAULT_EPISODES_KEEP_DAYS),
             "turns": ("BLUEAGENT_TURNS_KEEP_DAYS", DEFAULT_TURNS_KEEP_DAYS)}
    name, default = table.get(kind, ("BLUEAGENT_LOG_KEEP_DAYS", 30))
    return _days_from_env(name, default)


def daily_dir_for(path) -> pathlib.Path:
    """日檔放哪：`data/logs/daily/`（與活檔同層的 `daily/` 子目錄）。"""
    path = pathlib.Path(path)
    return (path.parent / "daily") if path.parent.name != "daily" else path.parent


def rotate_daily(path, *, keep_days: int, at: float | None = None,
                 folder=None) -> list[pathlib.Path]:
    """活檔如果不是今天的 ⇒ 改名成 `daily/<stem>-<YYYYMMDD>.jsonl`；回搬走的檔案。

    `keep_days=0` ＝ 不切（回空 list）。搬完之後活檔是 0 byte 的新檔（路徑不變）。
    """
    if keep_days <= 0:
        return []
    path = pathlib.Path(path)
    with _lock_for(path):
        try:
            stat = path.stat()
        except OSError:
            return []
        if stat.st_size == 0:
            return []
        stamp = day_key(stat.st_mtime if at is None else at)
        if stamp == day_key(at):
            return []
        target_dir = pathlib.Path(folder) if folder is not None else daily_dir_for(path)
        target = target_dir / f"{path.stem}-{stamp}{path.suffix}"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if target.exists():                      # 同一天已經有日檔 ⇒ 併進去，不蓋掉
                with target.open("ab") as sink, path.open("rb") as source:
                    sink.write(source.read())
                path.write_text("", encoding="utf-8")
            else:
                os.replace(path, target)
                path.touch()
        except OSError:
            return []
        return [target]


def prune_daily(path, *, keep_days: int, folder=None, at: float | None = None,
                compress: bool = False) -> list[pathlib.Path]:
    """刪掉超過保留期的日檔（`compress=True` 時把還沒過期的舊檔壓成 `.gz`）。回刪掉的路徑。"""
    if keep_days <= 0:
        return []
    path = pathlib.Path(path)
    target_dir = pathlib.Path(folder) if folder is not None else daily_dir_for(path)
    if not target_dir.is_dir():
        return []
    cutoff = day_key((at if at is not None else time.time()) - keep_days * 86400)
    removed: list[pathlib.Path] = []
    for child in sorted(target_dir.glob(f"{path.stem}-*{path.suffix}*")):
        suffix = child.name[len(path.stem) + 1:]
        stamp = suffix.split(".")[0]
        if len(stamp) != 8 or not stamp.isdigit():
            continue
        if stamp < cutoff:
            try:
                child.unlink()
                removed.append(child)
            except OSError:
                continue
    return removed


def daily_summary(path, *, folder=None) -> dict:
    """日檔概況（`/api/metrics` 用）：幾個檔、最舊／最新、總位元組。"""
    path = pathlib.Path(path)
    target_dir = pathlib.Path(folder) if folder is not None else daily_dir_for(path)
    out = {"files": 0, "bytes": 0, "oldest": "", "newest": ""}
    if not target_dir.is_dir():
        return out
    stamps = []
    for child in sorted(target_dir.glob(f"{path.stem}-*{path.suffix}*")):
        suffix = child.name[len(path.stem) + 1:]
        stamp = suffix.split(".")[0]
        if len(stamp) != 8 or not stamp.isdigit():
            continue
        try:
            out["bytes"] += child.stat().st_size
        except OSError:
            continue
        stamps.append(stamp)
    out["files"] = len(stamps)
    if stamps:
        out["oldest"], out["newest"] = min(stamps), max(stamps)
    return out


class DailyGuard:
    """掛在「每寫一行」的呼叫端：每 `every` 次看一眼日界，跨日就切、順手清過期。"""

    def __init__(self, path, *, kind: str = "logs", keep_days: int | None = None,
                 folder=None, every: int = DEFAULT_EVERY, compress: bool = False) -> None:
        self.path = pathlib.Path(path)
        self.kind = str(kind)
        self.keep_days = daily_keep_days(self.kind) if keep_days is None else int(keep_days)
        self.folder = folder
        self.every = max(1, int(every))
        self.compress = bool(compress)
        self.rotations = 0
        self.pruned = 0
        self._count = 0
        self._lock = threading.Lock()

    def note_write(self) -> bool:
        with self._lock:
            self._count += 1
            due = self.keep_days > 0 and (self._count % self.every == 0)
        if not due:
            return False
        return self.check()

    def check(self) -> bool:
        """真的跨日了嗎；是就切檔 + 清過期。回「這次有沒有切」。"""
        if self.keep_days <= 0:
            return False
        moved = rotate_daily(self.path, keep_days=self.keep_days, folder=self.folder)
        removed = prune_daily(self.path, keep_days=self.keep_days, folder=self.folder,
                              compress=self.compress)
        with self._lock:
            if moved:
                self.rotations += 1
            self.pruned += len(removed)
        return bool(moved)

    def summary(self) -> dict:
        info = daily_summary(self.path, folder=self.folder)
        info.update({"keep_days": self.keep_days, "rotations": self.rotations,
                     "pruned": self.pruned})
        return info


__all__ = ["DEFAULT_ACTIVITY_KEEP_DAYS", "DEFAULT_ATTENTION_KEEP_DAYS",
           "DEFAULT_EPISODES_KEEP_DAYS", "DEFAULT_EVERY", "DEFAULT_KEEP",
           "DEFAULT_MAX_BYTES", "DEFAULT_TURNS_KEEP_DAYS", "DailyGuard", "RotationGuard",
           "append_archive", "daily_dir_for", "daily_keep_days", "daily_summary",
           "day_key", "dir_bytes", "keep_from_env", "max_bytes_from_env", "month_key",
           "prune_daily", "rotate_daily", "rotate_if_needed", "usage"]
