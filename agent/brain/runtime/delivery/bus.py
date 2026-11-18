"""推送匯流排：讓「不是這一輪回覆」的東西也能到 App（SSE 長連）。

為什麼需要：語音那種回合是「先短回一句、真正的工作排隊做完再補」，補的那條沒有請求可以回——
HTTP 是請求／回應，沒有請求就沒有回應。所以 App 必須**常駐一條 SSE**（`GET /api/events`），
伺服器把後續交付、主動訊息、狀態變更推到這條線上。

界線：
  · 匯流排做「按 principal 分箱 + 有界佇列」**＋可重播**：每條訊息有單調 id，最近 N 條留在記憶體，
    重連時帶 `Last-Event-ID` ⇒ 把斷線期間漏的補回去（契約 v2，2026-10-22）。
  · 每個訂閱者有界（預設 64 條），滿了丢最舊——寧可讓 App 漏一條舊的，也不要撐爆記憶體。
  · 匯流排自己不記內容到磁碟（傳輸層）；有文字的交付經 `recorder` 落進 outbox，App 換角色／重開後從 `/api/turns` 補得回來。
  · **按端分流**（owner 2026-11-01：MR 頭顯自己一條管道）：訂閱時標明是哪一端（手機／MR）；
    交付照它的來源送回那一端（`mr:` 開頭只給 MR，其他——包括主動訊息——只給手機），重播也照這條。
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from collections import deque

from agent.brain.domain.client_end import PHONE, end_of_source

#: 每個訂閱者的佇列上限（條）。
_QUEUE_MAX = 64
#: 每個人留多少條「可以重播」的訊息（重連補洞用）。
_HISTORY_MAX = 200


class EventBus:
    def __init__(self, *, queue_max: int = _QUEUE_MAX,
                 history_max: int = _HISTORY_MAX, recorder=None, clock=time.time) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, dict[int, queue.Queue]] = {}
        #: 訂閱 id → 哪一端（`phone`／`mr`）
        self._ends: dict[int, str] = {}
        self._seq = itertools.count(1)
        #: 訊息的單調 id（重播用；跟訂閱 id 分開算，重連才有意義）。
        #: 從**啟動當下的毫秒**起算：重啟後的新訊息編號一定比重啟前的大——以前從 1 起算，
        #: App 帶著重啟前的大游標回來，重啟後那幾條（小編號）全被 `id > last_id` 濾掉（unity-review 2026-10-28）。
        self._event_seq = itertools.count(int(float(clock()) * 1000))
        #: 有文字的交付先交給它落盤（outbox），回的欄位（`turn_id`）併進訊息——App 才補拿得到
        self.recorder = recorder
        self._queue_max = max(8, int(queue_max))
        self._history: dict[str, deque] = {}
        self._history_max = max(0, int(history_max))

    # ── 訂閱 ────────────────────────────────────────────────────────────────
    def subscribe(self, principal: str, end: str = PHONE) -> tuple[int, queue.Queue]:
        """回 `(sub_id, queue)`；用完要 `unsubscribe()`。`end`＝哪一端（只收送到這一端的交付）。"""
        token = str(principal or "")
        with self._lock:
            bucket = self._subs.setdefault(token, {})
            sub_id = next(self._seq)
            channel: queue.Queue = queue.Queue(maxsize=self._queue_max)
            bucket[sub_id] = channel
            self._ends[sub_id] = str(end or PHONE)
            return sub_id, channel

    def unsubscribe(self, principal: str, sub_id: int) -> None:
        with self._lock:
            bucket = self._subs.get(str(principal or ""))
            if not bucket:
                return
            bucket.pop(int(sub_id), None)
            self._ends.pop(int(sub_id), None)
            if not bucket:
                self._subs.pop(str(principal or ""), None)

    # ── 發佈 ────────────────────────────────────────────────────────────────
    def publish(self, principal: str, frame: dict, *, end: str = "") -> int:
        """推給這個人在**那一端**的所有連線（同一人可能兩支手機同時開著）。回送達數。

        交付看來源（`mr:` 開頭＝MR，其他＝手機）；狀態事件可用 `end` 明確指定端末。
        """
        token = str(principal or "")
        frame = dict(frame or {})
        if self.recorder is not None:
            try:
                frame.update(self.recorder(token, frame) or {})
            except Exception:              # noqa: BLE001 - 落盤失敗不能擋推送
                pass
        with self._lock:
            envelope = {"id": next(self._event_seq), "at": round(time.time(), 3), **frame}
            end = str(end or "") or end_of_source(str(frame.get("source") or ""))
            if self._history_max > 0:
                bucket = self._history.get(token)
                if bucket is None:
                    bucket = deque(maxlen=self._history_max)
                    self._history[token] = bucket
                bucket.append({**envelope, "_end": end})
        delivered = 0
        with self._lock:
            channels = [channel for sub_id, channel in self._subs.get(token, {}).items()
                        if self._ends.get(sub_id, PHONE) == end]
        for channel in channels:
            try:
                channel.put_nowait(envelope)
                delivered += 1
            except queue.Full:
                try:                       # 丢最舊的，塞新的
                    channel.get_nowait()
                    channel.put_nowait(envelope)
                    delivered += 1
                except Exception:          # noqa: BLE001
                    pass
        return delivered

    # ── 重播（契約 v2：`Last-Event-ID`）────────────────────────────────────────
    def replay(self, principal: str, last_id: int, end: str = PHONE) -> list[dict]:
        """回「這個人、這一端、id 大於 last_id」的訊息（最多 `history_max` 條，依原順序）。

        `last_id <= 0` ⇒ 不回（第一次連線不需要補）。
        """
        if int(last_id or 0) <= 0:
            return []
        token = str(principal or "")
        with self._lock:
            bucket = list(self._history.get(token) or ())
        return [{key: value for key, value in item.items() if key != "_end"} for item in bucket
                if int(item.get("id") or 0) > int(last_id) and item.get("_end", PHONE) == str(end or PHONE)]

    def last_id(self, principal: str) -> int:
        token = str(principal or "")
        with self._lock:
            bucket = self._history.get(token)
            return int(bucket[-1]["id"]) if bucket else 0

    def counts(self, end: str = "") -> dict:
        """訂閱數；帶 `end` ⇒ 只算那一端的（主動訊息只送手機：只有 MR 開著不算「送得到」）。"""
        with self._lock:
            subscribers = sum(1 for bucket in self._subs.values() for sub_id in bucket
                              if not end or self._ends.get(sub_id, PHONE) == end)
            return {"principals": len(self._subs), "subscribers": subscribers}


__all__ = ["EventBus"]
