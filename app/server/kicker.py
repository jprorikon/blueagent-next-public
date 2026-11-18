"""App Server → Agent 的「推送叫醒」（規格：`docs/PLAN-app-kick-20261108.md`）。

為什麼要有這支：Agent 那側原本每個角色一條 0.35 s 輪詢（實測 8.55 次／秒、日誌 46 MB／天，
而且兩天就把業務日誌頂掉）。改成「事件落庫後叫一次，Agent 立刻拉一次」＋ Agent 側 20 s 兜底對賬。

三條紅線（照規格 §8，改之前先讀）：

  · **fire-and-forget**：0.2 s 逾時就丟，只記一行日誌，**絕不影響發消息、絕不拋給呼叫方**；
  · **不改變平台的獨立性**：kick 目標掛掉不能讓 `health` 變紅，也不能讓發消息失敗；
  · **只帶「誰該醒」**：不帶業務內容；Agent 一律以 `/v1/events` 為真值（重複 kick 無害）。

叫醒判據不是這裡發明的，是 owner 2026-11-08 逐條給的（見 `targets_for`）：

| 事件 | 踢誰 |
|---|---|
| `chat.message.created`（direct） | 會話裡的實體（排除作者） |
| `chat.message.created`（group） | 群裡全部實體（排除作者） |
| `post.created` | 不踢 |
| `reply.created` / `post.liked` | 帖主（實體）；帖主是人類 ⇒ 不踢 Agent，另有只給帖主的 `notice.created`（B.App.8） |
| `post.deleted` | 不是帖主自己刪的 ⇒ 踢帖主 |
| `location.*` / `mood.updated` | 不踢（一般公共事件，定期讀） |
| `chat.read.updated` | 不踢（已讀不急；Agent 兜底對賬時拉到，見 PLAN-common-ground §7.4） |
| `notice.created` | 不踢（給人類帖主的通知，App 內提醒條／系統推送讀它） |
| `call.incoming` / `call.updated` | 不踢（她打給主人的來電；只給主人的端末） |
"""

from __future__ import annotations

import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor

#: 判據表裡「不叫醒任何人」的事件（寫出來是為了讓漏掉的事件**看得見**）
SILENT_EVENTS = frozenset({
    "post.created", "location.updated", "location.revoked", "mood.updated",
    # 已读不急：Agent 20 s 兜底对账时拉到就够（主动消息的间隔是分钟级）
    "chat.read.updated",
    # 收回一則訊息：不急著叫醒她（下次讀房間時看得到墓碑）
    "chat.message.deleted",
    # 人類帖主的通知（B.App.8）：只給那個人看，跟 Agent 無關
    "notice.created",
    # 她打給主人的來電與狀態（給主人的端末；打電話的是她自己，不用叫醒任何 Agent）
    "call.incoming", "call.updated",
})
#: 帖類事件：判據是「帖主」，payload 形狀不同但都要現查 post 表
POST_EVENTS = frozenset({"reply.created", "post.liked", "post.deleted"})


def targets_for(*, event_type: str, payload: dict, actor_id: str, actor_kind: str,
                lookup_post, is_entity) -> list[str]:
    """回「這條事件要叫醒哪些實體」。純函式（除了兩個注入的查詢），好測。

    `lookup_post(post_id) -> (author_id, author_kind) | None`
    `is_entity(account_id) -> bool`
    """
    payload = payload or {}
    actor_id = str(actor_id or "")
    if event_type == "chat.message.created":
        participants = [str(item) for item in (payload.get("participants") or [])]
        # 一對一與群聊同一個事件型：差別只在 kind。群聊是「每個人自己決定拿不拿起手機」，
        # 所以兩者都是「參與者裡的實體」，只是群聊參與者本來就有多人。
        return [item for item in participants
                if item != actor_id and is_entity(item)]
    if event_type in POST_EVENTS:
        post_id = str(payload.get("post_id") or (payload.get("post") or {}).get("id") or "")
        if not post_id:
            return []
        author = lookup_post(post_id)
        if not author:
            return []
        author_id, author_kind = str(author[0] or ""), str(author[1] or "")
        if author_kind != "entity":
            # 帖主是人類 ⇒ 不叫醒 Agent；他的通知是 social service 寫的 `notice.created`（B.App.8）
            return []
        if event_type == "post.deleted" and actor_id == author_id:
            return []                      # 自己刪的不用叫醒自己
        return [author_id]
    return []                              # post.created 與 location／mood：不踢


class EventsKicker:
    """把「誰該醒」POST 給 Agent（`POST /api/app/kick`，`X-App-Kick-Token`）。"""

    def __init__(self, url: str = "", token: str = "", *, log=None,
                 timeout_s: float = 0.2) -> None:
        self.url = str(url or "").strip()
        self.token = str(token or "")
        self._log = log or (lambda _message: None)
        self.timeout_s = float(timeout_s)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="app-kick") \
            if self.url else None

    @classmethod
    def from_env(cls, *, log=None) -> "EventsKicker":
        return cls(os.environ.get("BLUEAGENT_APP_KICK_URL") or "",
                   os.environ.get("BLUEAGENT_APP_KICK_TOKEN") or "", log=log)

    def enabled(self) -> bool:
        return bool(self.url) and self._pool is not None

    def kick(self, entities, *, reason: str = "") -> None:
        targets = [item for item in dict.fromkeys(str(e) for e in (entities or [])) if item]
        if not targets:
            return
        if not self.enabled():
            self._log(f"kick: 未設定 BLUEAGENT_APP_KICK_URL（跳過 {reason} {targets}）")
            return
        pool = self._pool
        if pool is None:
            return
        pool.submit(self._post, targets, str(reason or ""))

    def _post(self, entities: list[str], reason: str) -> None:
        body = json.dumps({"entities": entities, "reason": reason},
                          ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "X-App-Kick-Token": self.token})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                response.read(0)           # 只確認收下，不看內容（kick 不帶業務）
        except Exception as exc:           # noqa: BLE001 - 紅線：叫不醒不能影響發消息
            self._log(f"kick: {reason} {entities} 失敗（{type(exc).__name__}: {exc}）")

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None


__all__ = ["EventsKicker", "SILENT_EVENTS", "POST_EVENTS", "targets_for"]
