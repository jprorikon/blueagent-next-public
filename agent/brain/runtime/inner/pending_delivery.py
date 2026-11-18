"""主人要她交付、她還沒交的東西（2026-11-08）。

由來：aster 那條「観測記録送ってくれる？チェックするから」——她那一輪回空、之後四小時
反覆催主人「次、オーナーの番」。**她把自己的欠帳記到對方頭上了**：主人要的是東西，
不是她的批改。所以這裡把「主人要的交付」記成一筆 open loop：

  · 主人說話、句子裡有交付請求 ⇒ `note_request()` 記一筆（原文照存，可回溯）
  · 她**真的送出**（`photo`／`artifact` 幀）⇒ `clear_delivered()` 清掉
  · prompt 兩處看得到：回話那一輪（`[[todo]]`）與心跳組句（ground 一行）
  · 太久沒交（預設 3 天）⇒ 標 `expired_at`，不再進 prompt（不刪，留著查）

只有記帳：**不擋回話、不扣預算、不自己發訊息**。判斷「像不像交付請求」是規則（不是模型），
所以每一筆都把原文留著——誤判一眼看得出來，也不會靜默改動任何交付行為。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
import uuid

#: 交付請求的規則（日文＋中文）。寧可少收：抓不準就當沒有，不要把她每一句都變成欠帳。
REQUESTS: tuple[str, ...] = (
    "送って", "送る？", "送れ", "送ってくれ",
    "見せて", "出して", "渡して", "添付", "貼って", "貼っといて",
    "チェックするから", "チェックして", "確認するから", "確認したい",
    "发我", "发给我", "传给我", "给我看", "我看一下", "我看看", "检查一下", "发过来",
)

#: 超過這個長度就不當成「一句請求」（長文多半是在講事，不是在要東西）
MAX_REQUEST_CHARS = 200

#: 預設幾天沒交就過期（主人要的東西不會永遠有效；過期只是不再提，紀錄留著）
DEFAULT_TTL_DAYS = 3.0


def looks_like_delivery_request(text: str) -> str:
    """命中的關鍵詞；不像就回空字串。

    **不做語意判斷**：這是規則層，寧可漏（少記一筆）也不要誤記。
    """
    body = str(text or "").strip()
    if not body or len(body) > MAX_REQUEST_CHARS:
        return ""
    for word in REQUESTS:
        if word in body:
            return word
    return ""


def _norm(text: str) -> str:
    """去重用的指紋：全形空白收斂、頭尾去白。"""
    return " ".join(str(text or "").replace("\u3000", " ").split())


def _when(at: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(float(at or 0)))


class PendingDeliveryStore:
    """`data/pending_delivery/<entity>.json`——一個角色一份，很小，純檔案 I/O。"""

    def __init__(self, root, *, clock=time.time, ttl_days: float = DEFAULT_TTL_DAYS) -> None:
        self.root = pathlib.Path(root)
        self._clock = clock
        self.ttl_s = max(0.0, float(ttl_days)) * 86400.0
        #: 回話執行緒在記、心跳執行緒在讀 ⇒ 讀改寫整段鎖住（跟 reminders／tasks 同一個理由）
        self._lock = threading.RLock()

    # ── 檔案 ────────────────────────────────────────────────────────────────
    def path_for(self, entity: str) -> pathlib.Path:
        return self.root / f"{entity}.json"

    def _load(self, entity: str) -> list[dict]:
        path = self.path_for(entity)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return []
        items = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        return [row for row in items if isinstance(row, dict)]

    def _save(self, entity: str, items: list[dict]) -> None:
        path = self.path_for(entity)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{pathlib.Path(str(uuid.uuid4())).name[:8]}.tmp")
        payload = {"entity": str(entity), "updated_at": self._clock(), "items": items}
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        # 容器以 root 跑、主機側工具以 986 跑 ⇒ 寫完就交還給父目錄的擁有者
        # （`runtime/inner` 不准橫向 import 其他 runtime 職責，所以這裡用 stdlib 做同一件事）
        try:
            parent = path.parent.stat()
            os.chown(path, parent.st_uid, parent.st_gid)
        except (OSError, AttributeError):                  # noqa: BLE001 - 權限收尾不能擋記帳
            pass

    # ── 記帳 ────────────────────────────────────────────────────────────────
    def note_request(self, entity: str, principal: str, text: str, *, at: float | None = None,
                     source: str = "", turn: str = "") -> dict | None:
        """主人要她交付 ⇒ 記一筆。不像請求／已有一模一樣的未交項 ⇒ 回 None。"""
        matched = looks_like_delivery_request(text)
        if not matched:
            return None
        body = str(text or "").strip()
        now = float(at if at is not None else self._clock())
        with self._lock:
            items = self._load(entity)
            fingerprint = _norm(body)
            for row in items:
                if row.get("cleared_at") or row.get("expired_at"):
                    continue
                if _norm(row.get("what") or "") == fingerprint:
                    return None                        # 同一句再要一次不算新欠帳
            row = {
                "id": "dl_" + uuid.uuid4().hex[:10],
                "what": body[:200],
                "matched": matched,
                "principal": str(principal or ""),
                "asked_at": now,
                "source": str(source or ""),
                "turn": str(turn or ""),
                "cleared_at": 0.0,
                "expired_at": 0.0,
                "reason": "",
            }
            items.append(row)
            self._save(entity, items)
        return row

    def _expire(self, items: list[dict], *, now: float) -> bool:
        """超過 TTL 的未交項就地標 expired（不刪）。回有沒有改到。"""
        if self.ttl_s <= 0:
            return False
        changed = False
        for row in items:
            if row.get("cleared_at") or row.get("expired_at"):
                continue
            asked = float(row.get("asked_at") or 0.0)
            if asked and now - asked > self.ttl_s:
                row["expired_at"] = now
                row["reason"] = "expired"
                changed = True
        return changed

    def items(self, entity: str, *, principal: str | None = None) -> list[dict]:
        """還開著的欠帳（未交、未過期），時間序。"""
        now = self._clock()
        with self._lock:
            items = self._load(entity)
            if self._expire(items, now=now):
                self._save(entity, items)
            out = []
            for row in items:
                if row.get("cleared_at") or row.get("expired_at"):
                    continue
                if principal and str(row.get("principal") or "") not in {"", str(principal)}:
                    continue
                out.append(dict(row))
            out.sort(key=lambda row: float(row.get("asked_at") or 0.0))
            return out

    def clear_delivered(self, entity: str, *, principal: str | None = None,
                        reason: str = "delivered") -> int:
        """她真的送出去了 ⇒ 清掉。回清了幾筆。"""
        now = self._clock()
        with self._lock:
            items = self._load(entity)
            cleared = 0
            for row in items:
                if row.get("cleared_at") or row.get("expired_at"):
                    continue
                if principal and str(row.get("principal") or "") not in {"", str(principal)}:
                    continue
                row["cleared_at"] = now
                row["reason"] = str(reason or "delivered")
                cleared += 1
            if cleared:
                self._save(entity, items)
            return cleared

    # ── 給 prompt ───────────────────────────────────────────────────────────
    def render(self, entity: str, *, principal: str | None = None, limit: int = 3) -> str:
        """一行一筆：「・『原文』（什麼時候要的）」。沒有就回空字串。"""
        rows = self.items(entity, principal=principal)[:max(1, int(limit))]
        return "\n".join(f"・「{str(row.get('what') or '')[:120]}」（{_when(row.get('asked_at') or 0)}）"
                         for row in rows)


__all__ = ["DEFAULT_TTL_DAYS", "MAX_REQUEST_CHARS", "PendingDeliveryStore",
           "REQUESTS", "looks_like_delivery_request"]
