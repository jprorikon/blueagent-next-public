"""三人房的定时敲门（GROUP-SELF-CHAT §2 ①，2026-11-08 后台报告「没人说话时不会自己醒来敲门」）。

M5a／M5b 只有「新消息」这一个入口：没人说话的时候谁都不会拿起手机，
到达时没被看到的消息之后也再没人看到。这里补上第二个入口——每人约 5 分钟（±150 秒）敲一次门：

  1. 按同一套拿起率（`GroupAttention.probability(notify=False)`：时段×忙闲×距上次看到、
     生物钟、owner 地板）决定这一次有没有看手机。没看 ⇒ 什么都没发生。
  2. 看了，房里有她还没看过的消息 ⇒ 对最新那一条走正常的群聊回合（名额、上限照旧）；
     更早没看过的几则由回合的 `[[room]]` 一并附上（`adapter/app_read_tools.room_note`），再往前她用 `app_chat_read` 自己读。
  3. 看了，房里已经安静 `QUIET_S` 以上 ⇒ 问她自己要不要开个话题（`opener.compose`，多半是不说）；
     说了就占一个名额、过泡泡上限、发出去，并写回她自己的群聊线（不然下一轮她不知道自己说过）。

开场的问法每人每 `OPENER_TRY_GAP_S` 最多一次（每次都是一次 LLM 调用）。
"""

from __future__ import annotations

from agent.adapter import app_group_delay
from agent.adapter.app_group import OPENER_TRY_GAP_S, QUIET_S, knock_delay, knock_roll

__all__ = ["knock_tick", "OPENER_TRY_GAP_S"]


def _text(message: dict) -> str:
    parts = [part for part in (message.get("parts") or []) if isinstance(part, dict)]
    words = [str(part.get("text") or "").strip() for part in parts if str(part.get("text") or "").strip()]
    if words:
        return " ".join(words)
    return "（画像）" if any(str(part.get("kind") or "") == "photo" for part in parts) else ""


def _author(message: dict) -> tuple[str, str]:
    author = message.get("author") or {}
    return str(author.get("id") or message.get("author_id") or ""), str(author.get("kind") or "")


def _rooms(adapter) -> list[str]:
    """已知的群（看过群消息就记在账本里）；还没有就问一次 App Server。"""
    rooms = adapter.group_ledger.rooms()
    if rooms:
        return rooms
    data = adapter.client.board_call("chat.conversations", {})
    rows = data if isinstance(data, list) else (data or {}).get("conversations") or []
    for row in rows:
        if isinstance(row, dict) and str(row.get("kind") or "") == "group" and row.get("id"):
            adapter.group_ledger.remember_room(str(row["id"]), row.get("participants") or ())
    return adapter.group_ledger.rooms()


def knock_tick(adapter, now: float) -> str:
    """到点就敲第一个无延迟回合的房；同房间暂停避免对同批消息重复起回合。"""
    if now < adapter._next_knock:
        return "wait"
    rooms = _rooms(adapter)
    paused = app_group_delay.pending_rooms(adapter)
    available = [room for room in rooms if room not in paused]
    if rooms and not available:
        return "pending"                  # 不推进下次敲门；延迟结算后立刻还能敲
    adapter._next_knock = now + knock_delay(adapter.entity, now)
    return _knock(adapter, available[0], now) if available else "no_room"


def _knock(adapter, conversation_id: str, now: float) -> str:
    entity, ledger = adapter.entity, adapter.group_ledger
    last_seen = ledger.last_seen(conversation_id, entity)
    p, _kind = adapter.group_attention.probability(
        entity, now, last_seen_at=last_seen,
        owner_recent=ledger.owner_recent(conversation_id, now), notify=False)
    if knock_roll(entity, conversation_id, now) >= p:
        return "not_seen"
    rows = adapter.client.board_call("chat.messages", {"conversation_id": conversation_id,
                                                       "limit": 12}).get("messages") or []
    unseen = [row for row in rows if float(row.get("created_at") or 0.0) > last_seen
              and _author(row)[0] != entity]
    if not unseen:
        ledger.mark_seen(conversation_id, entity, now)
    else:
        # 補看路徑：「看到哪」留給回合去推進——回合要靠舊的 last_seen 判斷哪幾則她還沒讀過
        # （[[room]] 只附那之後的）；先標成 now 的話那一段就成了空的。回合後再補到 now。
        latest = unseen[-1]
        adapter.log(f"app adapter[{entity}]: 敲门看到 {len(unseen)} 则没看过的 ⇒ 回最新一则（其余附在 [[room]]）")
        event = {"type": "chat.message.created", "_knock": True,
                 "event_id": "knock:" + str(latest.get("id") or ""),
                 "payload": {"conversation_id": conversation_id,
                             "conversation_kind": "group",
                             "participants": ledger.participants(conversation_id),
                             "message": latest}}
        adapter._handle(event)
        if not event.get("_enqueued_key"):
            ledger.mark_seen(conversation_id, entity, now)
        # 入队了（2026-11-16 起群聊回合一律入队）⇒ 「看到哪」由那一轮执行时推进：
        # 这里先标成 now 的话，回合跑的时候 [[room]] 的「没看过的那几则」就成了空的
        return "caught_up"
    if adapter.opener is None:
        return "quiet"
    last_at = max((float(row.get("created_at") or 0.0) for row in rows), default=0.0)
    if last_at and now - last_at < QUIET_S:
        return "busy"
    if not ledger.opener_due(conversation_id, entity, now):
        return "opener_wait"
    ledger.note_opener_try(conversation_id, entity, now)
    granted, reason = ledger.reserve(conversation_id, f"opener:{entity}:{int(now)}", entity,
                                     at=now, from_ai=False, owner=False)
    if not granted:
        return reason
    lines = [(adapter.speaker_name(*_author(row)), _text(row)) for row in rows[-8:] if _text(row)]
    text = str(adapter.opener.compose(entity, lines, (now - last_at) if last_at else 0.0) or "").strip()
    if not text:
        return "declined"                    # 多数时候是这个：她看了一眼，没什么要说的
    if not ledger.commit(entity, 1):
        return "quota"
    adapter.client.send_message(conversation_id, [text], mutation_id=f"agent-opener:{entity}:{int(now)}",
                                causation_id="")
    adapter.opener.record(entity, conversation_id, text, now, ledger.participants(conversation_id))
    adapter.log(f"app adapter[{entity}]: 房里安静 ⇒ 自己开了个话题")
    return "opened"
