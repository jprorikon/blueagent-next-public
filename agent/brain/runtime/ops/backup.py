"""每日資料快照（owner 2026-10-23 第 1 項）：她的記憶現在只有一份，先加保險。

備份什麼（只備「沒有它她就變另一個人」的東西）：

    data/memory/facts.db        事實＋履歷（含 window_summaries）
    data/threads.json           會話線（含 carry_summary）
    data/raw/<entity>/*.jsonl   原文倉
    data/memory/windows/        提煉窗口摘要
    data/prethink/              閒時預想
    data/users/*.md             每個人的 user.md（身分與偏好）

不備份：logs／activities／media／backups 自己（可重建或太大）。

策略：
  · 每天一份 `data/backups/<YYYY-MM-DD>/`（同一天再跑就覆蓋）
  · 保留 `keep_days`（預設 7）份，更舊的整包刪掉
  · 單檔超過 `max_file_mb` 的跳過（避免把整個備份塞爆）；跳過的會記在 `MANIFEST.json`
  · 全部只讀取、不改原檔；失敗不拋（回 `{ok: False, error}`）
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import threading
import time

DEFAULT_KEEP_DAYS = int(os.environ.get("BLUEAGENT_BACKUP_KEEP_DAYS") or 7)
DEFAULT_MAX_FILE_MB = float(os.environ.get("BLUEAGENT_BACKUP_MAX_FILE_MB") or 512)


class DataBackup:
    def __init__(self, data_dir, *, root=None, keep_days: int = DEFAULT_KEEP_DAYS,
                 max_file_mb: float = DEFAULT_MAX_FILE_MB, clock=time.time) -> None:
        self.data = pathlib.Path(data_dir)
        self.root = pathlib.Path(root) if root is not None else self.data / "backups"
        self.keep_days = max(1, int(keep_days))
        self.max_bytes = int(max_file_mb * 1024 * 1024)
        self._clock = clock
        self._lock = threading.RLock()
        self.last: dict = {}

    # ── 要備份的清單 ────────────────────────────────────────────────────────
    def _targets(self) -> list[pathlib.Path]:
        items = [
            self.data / "memory" / "facts.db",
            self.data / "threads.json",
            self.data / "llm.json",
            self.data / "mcp.json",
            # 帳號層（2026-10-31 補）：`accounts.json` 是 **App 建的 guest 唯一的家**——
            # 它不在 `guests.json` 的 legacy 名單裡，一旦這個檔壞掉／被蓋掉，那些帳號就救不回來
            # （啟動時的遷移只會從 `auth.json` ＋ `guests.json` 重建出 owner 與舊 guest）。
            self.data / "accounts.json",
            self.data / "guests.json",
            self.data / "auth.json",
        ]
        for pattern in ("raw/*/*.jsonl", "memory/windows/*/*.json", "prethink/*.json",
                        "users/*.md"):
            items.extend(sorted(self.data.glob(pattern)))
        return [path for path in items if path.is_file()]

    # ── 跑一次 ──────────────────────────────────────────────────────────────
    def run_once(self, *, now: float | None = None) -> dict:
        stamp = time.strftime("%Y-%m-%d", time.localtime(float(now if now is not None
                                                               else self._clock())))
        with self._lock:
            folder = self.root / stamp
            folder.mkdir(parents=True, exist_ok=True)
            copied: list[str] = []
            skipped: list[dict] = []
            total = 0
            for path in self._targets():
                rel = path.relative_to(self.data)
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if self.max_bytes and size > self.max_bytes:
                    skipped.append({"path": str(rel), "reason": "too_large", "bytes": size})
                    continue
                target = folder / rel
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                except OSError as exc:
                    skipped.append({"path": str(rel), "reason": f"{type(exc).__name__}"})
                    continue
                copied.append(str(rel))
                total += size
            manifest = {"date": stamp, "at": round(self._clock(), 3), "files": len(copied),
                        "bytes": total, "copied": copied, "skipped": skipped,
                        "keep_days": self.keep_days}
            manifest["pruned"] = self._prune_locked(now=now)
            manifest["ok"] = True
            try:
                (folder / "MANIFEST.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
            self.last = manifest
            return manifest

    def _prune_locked(self, *, now: float | None = None) -> list[str]:
        current = time.localtime(float(now if now is not None else self._clock()))
        cutoff = time.localtime(self._clock() - self.keep_days * 86400)
        cutoff_key = time.strftime("%Y-%m-%d", cutoff)
        removed: list[str] = []
        if not self.root.exists():
            return removed
        for folder in sorted(self.root.iterdir()):
            if not folder.is_dir():
                continue
            name = folder.name
            if len(name) == 10 and name[4] == "-" and name < cutoff_key:
                try:
                    shutil.rmtree(folder)
                    removed.append(name)
                except OSError:
                    continue
        del current
        return removed

    def snapshot(self) -> dict:
        rows = []
        if self.root.exists():
            for folder in sorted(self.root.iterdir()):
                if folder.is_dir():
                    manifest = folder / "MANIFEST.json"
                    try:
                        body = json.loads(manifest.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        body = {}
                    rows.append({"date": folder.name, "files": body.get("files"),
                                 "bytes": body.get("bytes")})
        return {"root": str(self.root), "keep_days": self.keep_days, "backups": rows[-10:],
                "last": self.last}


def start_worker(backup: DataBackup, *, interval_s: float = 86400.0) -> threading.Thread:
    """背景執行緒：開機先跑一次，之後每 interval（預設 24h）再跑。"""
    def loop() -> None:
        while True:
            try:
                backup.run_once()
            except Exception as exc:                        # noqa: BLE001
                backup.last = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            time.sleep(max(600.0, float(interval_s)))

    thread = threading.Thread(target=loop, name="backup", daemon=True)
    thread.start()
    return thread
