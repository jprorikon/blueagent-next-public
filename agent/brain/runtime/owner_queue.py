"""主人指令的佇列：**一次只跑一條**，每條有編號、狀態、結果摘要；落盤、可取消、有上限。

為什麼要這個（不是排程器已經有了嗎）：

  · 排程器的「同線串行」是按 thread 算的。主人從 App、Discord DM、語音頻道各發一次，
    那是**三條線**⇒ 三件事同時在做，他看不到「誰先誰後、做到哪」。
  · 指令是「他交代的事」，應該有編號與狀態，而且要能事後查（`GET /api/commands`）。
    排程器只保證不撞車，不管交代。

界線：
  · 只收**主人的工作指令**（work）。閒聊不走這裡——閒聊要即時，排隊會讓人覺得她卡住。
  · 佇列按角色分（`mira` 的事不擋 `aster` 的事），同一個角色嚴格 FIFO。
  · 執行本體是呼叫端給的 `run(text, entity, principal, meta) -> dict`；
    這裡只排隊、記狀態、留最近 N 條結果。

owner 2026-11-16 定案（PENDING B.Agent.0，原 A.1）：
  ① 入口：API（`POST /api/command`）與聊天裡的 work 後續同一個佇列；Unity 收件箱 UI 之後再接。
  ② **取消在步邊界停**：還在排隊的立刻取消；正在跑的那一條**不殺**（殺到一半的工具呼叫才危險），
     跑完那一步就停——結果照記，但**不交付**，狀態是 `cancelled`。
     「步」＝一次工具呼叫或一段模型串流（2026-11-16 補）：執行中取消會翻那一輪的取消令牌，
     大腦的工具迴圈在下一個邊界（兩次工具呼叫之間、下一輪開頭）自己停；正在跑的那支工具跑完。
     執行本體從 `meta[CANCEL_HOOK]` 拿到登記函式，把「翻令牌」交回來（不落盤）。
  ③ **上限**：同一個角色未完成（排隊＋執行中）最多 10 條；排隊超過 24 小時作廢（`expired`）。
  ④ `trpg／game／read` 的「真動作」這輪只做骨架：`kind` 欄位收得下，但只有 `work` 會跑，
     其餘回 `kind_not_implemented`（不悄悄當成 work 跑）。

落盤（`store_path`，JSON，原子替換）：
  · 排隊中的重啟後照跑（過期的直接標 `expired`）；
  · **跑到一半被重啟打斷的標成 `failed`（`interrupted_by_restart`），不重跑**——
    它可能已經做了一半的事（發了帖、動了檔），重跑一次就是做兩次；
  · `on_done`（呼叫端的交付回呼）存不了：重啟後恢復的那幾條改用建構時給的 `deliver`；
  · `meta` 只存 JSON 存得下的部分（語音那條的 `on_speech_frame` 之類回呼會被丟掉）。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from collections import deque

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
#: 執行時塞進 `meta` 的登記函式鍵：`meta[CANCEL_HOOK](fn)` ⇒ 執行中被取消時呼叫 `fn()`（不落盤）
CANCEL_HOOK = "_on_cancel"
EXPIRED = "expired"
TERMINAL = frozenset({DONE, FAILED, CANCELLED, EXPIRED})

#: 現在真的會跑的指令種類；`PLANNED_KINDS` 收得下但還沒實作（owner 2026-11-16：這輪只做骨架）
KINDS = frozenset({"work"})
PLANNED_KINDS = frozenset({"trpg", "game", "read"})
#: 同一個角色未完成的上限、排隊的壽命（owner 2026-11-16：10 條／24 h）
MAX_PER_ENTITY = int(os.environ.get("BLUEAGENT_COMMAND_MAX_PER_ENTITY") or 10)
TTL_S = float(os.environ.get("BLUEAGENT_COMMAND_TTL_H") or 24) * 3600.0

_STORED = ("id", "text", "entity", "principal", "source", "kind", "meta", "state",
           "queued_at", "started_at", "finished_at", "error", "result_chars", "result_preview",
           "cancel_requested")
#: 收件箱（Unity `NextCommandsView`）看得到的長度：指令開頭、結果開頭（全文仍只在交付的那則訊息裡）
PREVIEW_CHARS = 80
RESULT_PREVIEW_CHARS = 160


def _json_safe(value):
    """只留 JSON 存得下的（回呼、物件一律丟掉）。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [item for item in (_json_safe(item) for item in value) if item is not _DROP]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            safe = _json_safe(item)
            if safe is not _DROP:
                out[str(key)] = safe
        return out
    return _DROP


_DROP = object()


class OwnerCommandQueue:
    def __init__(self, run, *, log=None, per_entity: bool = True,
                 max_keep: int = 50, max_pending: int = 50,
                 max_per_entity: int = MAX_PER_ENTITY, ttl_s: float = TTL_S,
                 store_path=None, deliver=None, clock=time.time) -> None:
        """`deliver(item, result)`：重啟後恢復的那幾條（沒有 `on_done` 了）做完時怎麼交付。"""
        self._run = run
        self._log = log or (lambda _message: None)
        self._per_entity = bool(per_entity)
        self._max_keep = max(4, int(max_keep))
        self._max_pending = max(1, int(max_pending))
        self._max_per_entity = max(1, int(max_per_entity))
        self._ttl_s = max(1.0, float(ttl_s))
        self._clock = clock
        self._deliver = deliver
        self._path = pathlib.Path(store_path) if store_path else None
        self._lock = threading.RLock()
        self._items: "deque[dict]" = deque(maxlen=self._max_keep)
        self._pending: list[dict] = []
        self._next_id = 1
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._running: dict[str, dict] = {}
        self.submitted = 0
        self.finished = 0
        self.failed = 0
        self.rejected = 0
        self.cancelled = 0
        self._load()

    # ── 生命週期 ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, daemon=True,
                                        name="owner-commands")
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ── 對外 ────────────────────────────────────────────────────────────────
    def submit(self, text: str, *, entity: str = "", principal: str = "owner",
               source: str = "", meta: dict | None = None, kind: str = "work",
               on_done=None) -> dict:
        """排一條指令。回 `{ok, id, queued, reason?}`；滿了就拒（不靜默丢）。"""
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "reason": "empty"}
        kind = str(kind or "work").strip().lower()
        if kind not in KINDS:
            return {"ok": False, "reason": "kind_not_implemented" if kind in PLANNED_KINDS
                    else "kind_unknown", "kind": kind}
        entity = str(entity or "")
        with self._lock:
            self._expire_locked(float(self._clock()))
            open_for_entity = sum(1 for item in self._pending if item["entity"] == entity) + \
                (1 if entity and self._running.get(entity) else 0)
            if len(self._pending) >= self._max_pending or open_for_entity >= self._max_per_entity:
                self.rejected += 1
                return {"ok": False, "reason": "queue_full", "pending": len(self._pending),
                        "open_for_entity": open_for_entity}
            item = {"id": self._next_id, "text": body, "entity": entity,
                    "principal": str(principal or "owner"), "source": str(source or ""),
                    "kind": kind, "meta": dict(meta or {}), "state": QUEUED,
                    "queued_at": round(float(self._clock()), 3),
                    "started_at": None, "finished_at": None, "error": "",
                    "result_chars": 0, "cancel_requested": False, "on_done": on_done}
            self._next_id += 1
            self._pending.append(item)
            self.submitted += 1
            pending = len(self._pending)
            self._save_locked()
        self._wake.set()
        return {"ok": True, "id": item["id"], "queued": pending}

    def cancel(self, command_id) -> dict:
        """取消一條（步邊界停）：排隊中的立刻 `cancelled`；執行中的跑完那一步就停、不交付。"""
        try:
            wanted = int(command_id)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "not_found"}
        with self._lock:
            for item in self._pending:
                if item["id"] == wanted:
                    self._pending.remove(item)
                    self._finish_locked(item, CANCELLED)
                    self.cancelled += 1
                    self._save_locked()
                    return {"ok": True, "id": wanted, "state": CANCELLED}
            hooks = None
            for item in self._running.values():
                if item["id"] == wanted:
                    item["cancel_requested"] = True
                    hooks = list(item.get("cancel_hooks") or ())
                    self._save_locked()
                    break
            else:
                for item in self._items:
                    if item["id"] == wanted:
                        return {"ok": False, "reason": "already_finished", "state": item["state"]}
                return {"ok": False, "reason": "not_found"}
        for hook in hooks:                              # 翻令牌：工具迴圈在下一個邊界停（不殺執行緒）
            self._call_hook(item, hook)
        return {"ok": True, "id": wanted, "state": RUNNING, "cancel_requested": True}

    def _register_cancel(self, item: dict, hook) -> None:
        """執行本體交回「怎麼停這一輪」；已經被取消了就當場呼叫（登記晚於取消的競態）。"""
        if not callable(hook):
            return
        with self._lock:
            already = bool(item.get("cancel_requested"))
            if not already:
                item.setdefault("cancel_hooks", []).append(hook)
        if already:
            self._call_hook(item, hook)

    def _call_hook(self, item: dict, hook) -> None:
        try:
            hook()
        except Exception as exc:                        # noqa: BLE001 - 停不下來就等它跑完那一步
            self._log(f"command[{item['id']}]: 取消回呼失敗（{type(exc).__name__}: {exc}）")

    def snapshot(self, limit: int = 20) -> dict:
        with self._lock:
            self._expire_locked(float(self._clock()))
            done = [self._public(item) for item in list(self._items)[-max(1, int(limit)):]]
            running = [self._public(item) for item in self._running.values()]
            pending = [self._public(item) for item in self._pending]
        return {"pending": len(pending), "running": running,
                "queued": pending, "recent": done,
                "limits": {"per_entity": self._max_per_entity, "ttl_h": round(self._ttl_s / 3600, 2)},
                "stats": {"submitted": self.submitted, "finished": self.finished,
                          "failed": self.failed, "rejected": self.rejected,
                          "cancelled": self.cancelled}}

    # ── 內部 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _public(item: dict) -> dict:
        return {key: item.get(key) for key in
                ("id", "state", "kind", "entity", "principal", "source", "queued_at",
                 "started_at", "finished_at", "error", "result_chars", "cancel_requested")} | \
               {"chars": len(str(item.get("text") or "")),
                "preview": str(item.get("text") or "")[:PREVIEW_CHARS],
                "result_preview": str(item.get("result_preview") or "")}

    def _finish_locked(self, item: dict, state: str, error: str = "") -> None:
        item["state"] = state
        if error:
            item["error"] = error
        item["finished_at"] = round(float(self._clock()), 3)
        self._items.append(item)

    def _expire_locked(self, now: float) -> None:
        stale = [item for item in self._pending
                 if now - float(item.get("queued_at") or now) >= self._ttl_s]
        for item in stale:
            self._pending.remove(item)
            self._finish_locked(item, EXPIRED, "expired_in_queue")
            self._log(f"command[{item['id']}]: 排隊超過 {self._ttl_s / 3600:g} 小時 ⇒ 作廢")
        if stale:
            self._save_locked()

    def _pick_locked(self) -> dict | None:
        self._expire_locked(float(self._clock()))
        for item in self._pending:
            entity = str(item.get("entity") or "")
            if self._per_entity and entity and self._running.get(entity):
                continue                      # 同一個角色一次只跑一條
            return item
        return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                item = self._pick_locked()
                if item is not None:
                    self._pending.remove(item)
                    entity = str(item.get("entity") or "")
                    item["state"] = RUNNING
                    item["started_at"] = round(float(self._clock()), 3)
                    if entity:
                        self._running[entity] = item
                    self._save_locked()
            if item is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            self._execute(item)

    def _execute(self, item: dict) -> None:
        entity = str(item.get("entity") or "")
        result = None
        state, error = DONE, ""
        try:
            meta = dict(item.get("meta") or {})
            meta[CANCEL_HOOK] = lambda hook: self._register_cancel(item, hook)
            result = self._run(item["text"], entity=entity, principal=item["principal"], meta=meta)
            text = str((result or {}).get("text") or "") if isinstance(result, dict) else ""
            item["result_chars"] = len(text)
            item["result_preview"] = text[:RESULT_PREVIEW_CHARS]
        except Exception as exc:                        # noqa: BLE001 - 一條炸掉不能斷佇列
            state, error = FAILED, f"{type(exc).__name__}: {exc}"
        with self._lock:
            if state == DONE and item.get("cancel_requested"):
                state = CANCELLED                       # 步邊界：這一步跑完了，結果不交付
            if entity and self._running.get(entity) is item:
                self._running.pop(entity, None)
            item.pop("cancel_hooks", None)
            self._finish_locked(item, state, error)
            if state == DONE:
                self.finished += 1
            elif state == FAILED:
                self.failed += 1
            else:
                self.cancelled += 1
            self._save_locked()
        self._log(f"command[{item['id']}]: {state}"
                  + (f"（{item['result_chars']} 字）" if state == DONE else f"（{error or '已取消'}）"))
        if state != DONE:
            return
        hook = item.get("on_done")
        try:
            if hook is not None:
                hook(result)
            elif self._deliver is not None:
                self._deliver(self._public(item), result)   # 重啟後恢復的那幾條
        except Exception as exc:                        # noqa: BLE001 - 交付失敗不影響下一條
            self._log(f"command[{item['id']}]: 交付失敗（{type(exc).__name__}: {exc}）")

    # ── 落盤 ────────────────────────────────────────────────────────────────
    def _row(self, item: dict) -> dict:
        row = {key: item.get(key) for key in _STORED}
        meta = _json_safe(dict(item.get("meta") or {}))
        row["meta"] = meta if isinstance(meta, dict) else {}
        return row

    def _save_locked(self) -> None:
        if self._path is None:
            return
        rows = [self._row(item) for item in list(self._items) + list(self._running.values())
                + self._pending]
        payload = {"next_id": self._next_id, "items": rows}
        temporary = self._path.with_name(f".{self._path.name}.{threading.get_ident()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            self._log(f"commands: 落盤失敗（{type(exc).__name__}: {exc}）")

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._log(f"commands: 讀檔失敗、當成空佇列（{type(exc).__name__}）")
            return
        rows = [row for row in (payload.get("items") or []) if isinstance(row, dict)]
        interrupted = 0
        with self._lock:
            for row in rows:
                item = {key: row.get(key) for key in _STORED}
                item["meta"] = dict(row.get("meta") or {})
                item["on_done"] = None
                item["kind"] = str(item.get("kind") or "work")
                item["cancel_requested"] = bool(item.get("cancel_requested"))
                try:
                    item["id"] = int(item["id"])
                except (TypeError, ValueError):
                    continue
                if item["state"] == QUEUED:
                    self._pending.append(item)
                elif item["state"] == RUNNING:
                    # 跑到一半被重啟打斷：不重跑（可能已經做了一半的事）
                    self._finish_locked(item, FAILED, "interrupted_by_restart")
                    interrupted += 1
                else:
                    self._items.append(item)
            ids = [item["id"] for item in list(self._items) + self._pending]
            self._next_id = max([int(payload.get("next_id") or 1), *(i + 1 for i in ids)])
            self._pending.sort(key=lambda item: item["id"])
            self._expire_locked(float(self._clock()))
            self._save_locked()
        if self._pending or interrupted:
            self._log(f"commands: 重啟接續——排隊 {len(self._pending)} 條、打斷 {interrupted} 條（不重跑）")


__all__ = ["CANCEL_HOOK", "CANCELLED", "DONE", "EXPIRED", "FAILED", "KINDS", "PLANNED_KINDS", "QUEUED",
           "RUNNING", "TERMINAL", "OwnerCommandQueue"]
