"""她的行動日誌：**private log / public log**（owner 2026-10-22）。

為什麼要分兩種：

  · **private log**：只有她自己能回溯（心情、失敗、碎念、獨自的判斷）。
    誰都讀不到——包含主人。它不是秘密，是「她自己的房間」。
  · **public log**：可以公開的事（學習筆記、遊戲結果、做完的東西）。
    它會同時沉進記憶、**可被其他 agent 在回話時檢索**（aster／nori／外部 agent 都看得到），
    所以她跟別人相處時有共同的參照物，而不是每次都從零開始。

存放：`data/logs/activity-<entity>.jsonl`（append-only，一行一則，方便 grep 與回放）。
索引：public 那些另外寫進記憶（`self` 層、audience=None ⇒ 誰都讀得到）。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time

from agent.brain.runtime.ops.rotation import DailyGuard, RotationGuard

#: 公開與私密（刻意只有兩個值；要更多分級就再加，但別讓它變成權限系統）。
PRIVATE = "private"
PUBLIC = "public"


class ActivityLog:
    def __init__(self, root, *, public_sink=None, max_bytes: int | None = None,
                 keep: int | None = None) -> None:
        """`public_sink(entity, text, entry)`：public 那幾則要不要另外沉進記憶。"""
        self.root = pathlib.Path(root)
        self.public_sink = public_sink
        self._lock = threading.RLock()
        #: 每個角色一份檔 ⇒ 每個檔一組輪轉守衛（同一角色一天可能寫幾百則）。
        self._max_bytes = max_bytes
        self._keep = keep
        self._guards: dict[str, RotationGuard] = {}
        #: 分級輪轉（按天切 30 天）
        self._daily: dict[str, DailyGuard] = {}

    def path_for(self, entity: str) -> pathlib.Path:
        return self.root / f"activity-{str(entity or 'unknown')}.jsonl"

    def _guard(self, entity: str) -> RotationGuard:
        path = self.path_for(entity)
        guard = self._guards.get(str(path))
        if guard is None:
            guard = RotationGuard(path, max_bytes=self._max_bytes, keep=self._keep)
            self._daily[entity] = DailyGuard(path, kind="activity")
            self._guards[str(path)] = guard
        return guard

    @property
    def rotations(self) -> int:
        """這個程序啟動以來總共輪轉了幾次（`/api/metrics` 用）。"""
        return sum(guard.rotations for guard in self._guards.values())

    # ── 寫 ──────────────────────────────────────────────────────────────────
    def append(self, entity: str, *, kind: str, text: str, visibility: str = PRIVATE,
               activity: str = "", step: int = 0, extra: dict | None = None,
               activity_id: str = "") -> dict:
        """寫一條。

        `activity_id` 是**這一件**活動的 id（`Activity.id`）。去重只認它：
        同一件活動的同一格重跑（崩潰／重啟續跑）不會重複記帳，但**下一輪同名活動照記**。
        以前比的是標題＋步驟，於是「同一套活動重複做」在第二輪之後全被吞掉——公開日誌
        凍在第一輪，跟她實際做的事完全脫節（2026-10-24 實測）。
        """
        entry = {
            "at": round(time.time(), 3),
            "entity": str(entity),
            "kind": str(kind or ""),
            "activity": str(activity or ""),
            "activity_id": str(activity_id or ""),
            "step": int(step or 0),
            "visibility": PUBLIC if str(visibility) == PUBLIC else PRIVATE,
            "text": str(text or "").strip(),
        }
        if extra:
            entry.update({key: value for key, value in extra.items() if key not in entry})
        if not entry["text"]:
            return entry
        path = self.path_for(entity)
        with self._lock:
            # 去重：同**一件**活動的同一格（重試／重啟續跑）不重複記帳
            if entry["activity_id"]:
                for row in reversed(self.entries(entity, limit=64)):
                    if (row.get("activity_id") == entry["activity_id"]
                            and row.get("step") == entry["step"]
                            and row.get("visibility") == entry["visibility"]):
                        seen = dict(row)
                        seen["duplicate"] = True
                        return seen
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._guard(entity).note_write()
            daily = self._daily.get(entity)
            if daily is not None:
                daily.note_write()
        if entry["visibility"] == PUBLIC and self.public_sink is not None:
            try:
                self.public_sink(str(entity), entry["text"], entry)
            except Exception:                    # noqa: BLE001 - 索引失敗不能影響活動
                pass
        return entry

    # ── 讀 ──────────────────────────────────────────────────────────────────
    def entries(self, entity: str, *, limit: int = 50,
                visibility: str | None = None) -> list[dict]:
        path = self.path_for(entity)
        if not path.is_file():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if visibility is not None and row.get("visibility") != visibility:
                continue
            rows.append(row)
        return rows[-max(1, int(limit)):]

    @staticmethod
    def safe_text(row: dict) -> str:
        """給 prompt 用的那一句。角色之間的私聊（`kind=social`）只留「跟誰聊過」，**不留對方原話**：

        那是兩個人的房間；原話一進她的私記，就跟著私記進了主人那邊的回合、心跳的獨白與組句
        （DIAGNOSIS 2026-11-10 §2.3：aster 對她說的「……う」被寫成「主人は 623 分前…「……う」」）。
        """
        text = str(row.get("text") or "")
        if str(row.get("kind") or "") == "social":
            name = text.split("と私聊", 1)[0].strip() if "と私聊" in text else ""
            return (f"{name}と二人で話した" if name else "誰かと二人で話した") + "（中身は二人だけのこと）"
        return text

    def private_tail(self, entity: str, limit: int = 4) -> list[str]:
        """給她的 prompt 用的「最近私記」（很短，只夠她接上自己的心情）。"""
        return [self.safe_text(row) for row in self.entries(entity, limit=limit,
                                                            visibility=PRIVATE)]

    def public_tail(self, entity: str, limit: int = 6) -> list[str]:
        return [row["text"] for row in self.entries(entity, limit=limit,
                                                    visibility=PUBLIC)]

    def snapshot(self) -> dict:
        rows = []
        for path in sorted(self.root.glob("activity-*.jsonl")) if self.root.exists() else []:
            entity = path.stem.replace("activity-", "")
            entries = self.entries(entity, limit=10_000)
            public = sum(1 for row in entries if row.get("visibility") == PUBLIC)
            rows.append({"entity": entity, "entries": len(entries), "public": public,
                         "private": len(entries) - public, "path": str(path)})
        return {"logs": rows}


def quiet_hours(now: float | None = None, *, start: int = 23, end: int = 7) -> bool:
    """現在是不是「別吵」的時段（預設 23:00–07:00，本地時間）。"""
    hour = time.localtime(now if now is not None else time.time()).tm_hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


__all__ = ["ActivityLog", "PRIVATE", "PUBLIC", "quiet_hours"]
