"""群聊「拿起手机」延迟队列：把等待与事件拉取解耦（架构审核 E1／WP8，2026-11-15）。

为什么拆出来：
  以前在单线程事件循环里同步等待 12–90 秒再生成，后续私聊、位置更新、已读回执全被队头堵住。

现在的做法：
  1. 群聊决定「看到了」之后，先在 `GroupLedger` 占好名额（防定时敲门抢光）。
  2. 若 `decision.delay > 0`，把事件与名额快照原子写进 `<实体>-group-delay.json`，游标立即推进。
  3. `tick()` 只按注入时钟结算到期条目，不探测测试环境，也不执行真实等待。
     别人等待期间先回复不取消本轮；只有她自己已对更新的消息发言或占位、
     已入睡或泡泡超额才放弃。**光是读到后面的消息不算**（她可能只是旁观了别人的回复）。放弃与队列溢出都会释放名额并恢复占位前的台账快照。

私聊不抢占群聊回合（owner 2026-11-16 定 D3「不打断，她自己控制回复队列和周期」）；不做统一注意力状态机。

2026-11-16 回合移出拉取线程（PENDING B.App.2 剩余 ③）：
  · **所有**群聊回合都进这个队列（`delay=0` 的也一样，到期时刻＝现在）——拉取线程只做
    「判拿起、占名额、入队、推游标」，不再跑模型；
  · 生产上由 `AppChatAdapter` 的回合工作线程（`ba-app-<实体>-turns`）调 `tick()`；
    没起工作线程时（测试直接调 `poll_once()`）`poll_once` 照旧当场结算，语义不变；
  · 认领／移除在锁里、回合在锁外跑：跑一轮 30 秒期间拉取线程照样能入队，
    不会被 `tick` 结束时的整份覆写吃掉（旧写法是「读一次、跑完、写回 remaining」）。
"""

from __future__ import annotations

import threading

from agent.adapter.app_group import AttentionDecision
from agent.adapter.app_mentions import mention_kinds

SUFFIX = "group-delay"
MAX_ITEMS = 200
#: 测试替身没有自己的锁时共用这一把（真 adapter 在建构时给 `_delay_lock`）
_FALLBACK_LOCK = threading.RLock()


def _lock(adapter):
    return getattr(adapter, "_delay_lock", None) or _FALLBACK_LOCK


def _key(row: dict) -> str:
    return str(row.get("key") or "")


def _load(adapter) -> list[dict]:
    rows = (adapter.sidecar.read(SUFFIX) or {}).get("items")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _release(adapter, item: dict, reason: str) -> None:
    if item.get("granted"):
        adapter.group_ledger.release(
            str(item.get("conversation_id") or ""), str(item.get("message_id") or ""),
            adapter.entity, at=float(item.get("at") or 0.0),
            from_ai=bool(item.get("from_ai")), state=item.get("reservation_state"))
    adapter.log(f"app adapter[{adapter.entity}]: 群聊延迟放弃 "
                f"msg={item.get('message_id')} ({reason})")


def _save(adapter, items: list[dict]) -> None:
    # 保留最新条目；被挤掉的旧条目不能连名额一起丢掉。
    for item in items[:-MAX_ITEMS]:
        _release(adapter, item, "queue_overflow")
    adapter.sidecar.write(SUFFIX, {"items": items[-MAX_ITEMS:]})


def wait_s(adapter, now: float, default_s: float) -> float:
    """后台循环下一次醒来的等待秒数：有待到期群聊就按最早 `due_at` 缩短等待。"""
    items = _load(adapter)
    if not items:
        return float(default_s)
    earliest = min(float(row.get("due_at") or now) for row in items)
    return max(0.0, min(float(default_s), earliest - float(now)))


def hold(adapter, event: dict, *, conversation_id: str, message_id: str, at: float,
         delay: float, granted: bool, reason: str, from_ai: bool, call: str = "") -> str:
    """把 `delay > 0` 的群聊回合放进本角色的到期队列并持久化。回队列键 `key`。"""
    now = float(adapter._clock())
    key = str(message_id or event.get("event_id") or event.get("cursor") or "")
    with _lock(adapter):
        _hold_locked(adapter, event, key=key, now=now, conversation_id=conversation_id,
                     message_id=message_id, at=at, delay=delay, granted=granted,
                     reason=reason, from_ai=from_ai, call=call)
    event["_enqueued_key"] = key
    wake = getattr(adapter, "wake_turns", None)
    if callable(wake):
        wake()                                   # 回合工作线程：有新条目（含现在到期的）
    return key


def _hold_locked(adapter, event: dict, *, key: str, now: float, conversation_id: str,
                 message_id: str, at: float, delay: float, granted: bool, reason: str,
                 from_ai: bool, call: str) -> None:
    items = _load(adapter)
    if not any(str(row.get("key") or "") == key for row in items):
        clean = {k: v for k, v in event.items() if not str(k).startswith("_")}
        if event.get("_knock"):
            clean["_knock"] = True
        items.append({
            "key": key,
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
            "at": float(at),
            "delay": float(delay),
            "queued_at": now,
            "due_at": now + float(delay),
            "granted": bool(granted),
            "reason": str(reason),
            "from_ai": bool(from_ai),
            "call": str(call),
            "reservation_state": adapter.group_ledger.reservation_state(
                conversation_id, message_id, adapter.entity) if granted else None,
            "event": clean,
        })
        _save(adapter, items)


def group_turn(adapter, event: dict, conversation_id: str, message_id: str,
               message: dict, author: dict, text: str, *,
               knocked: bool = False, participants=()) -> bool | None:
    """群聊环境模型（M5a／M5b）：新消息先判拿起与占名额，然后**一律入队**，到期由回合工作线程跑。

    回 `None`＝没拿起手机或已放入队列（拉取线程不继续往下走）；
    到期执行时（`event["_delayed"]`）回 `False`＝只旁观、`True`＝拿到发言名额（讲不讲由大脑决定）。
    """
    owner = str(author.get("kind") or "") == "owner"
    at = float(message.get("created_at") or adapter._clock())
    ledger = adapter.group_ledger
    delayed = event.get("_delayed") if isinstance(event, dict) else None
    if isinstance(delayed, dict):
        adapter._seen_before = ledger.last_seen(conversation_id, adapter.entity)
        ledger.mark_seen(conversation_id, adapter.entity, at)
        granted = bool(delayed.get("granted"))
        reason = str(delayed.get("reason") or "")
        if not granted:
            adapter.log(f"app adapter[{adapter.entity}]: 群聊旁观 msg={message_id} ({reason})")
        return granted

    ledger.remember_room(conversation_id, participants)
    ledger.note_message(conversation_id, owner=owner, at=at, message_id=message_id)
    kinds = mention_kinds(text, adapter.aliases)
    call = kinds.get(adapter.entity, "")
    decision = AttentionDecision(True, 0.0, "active") if knocked else \
        ledger.decision(conversation_id, message_id or str(at), adapter.entity, lambda: (
            adapter.group_attention.decision(
                adapter.entity, message, call=call,
                last_seen_at=ledger.last_seen(conversation_id, adapter.entity),
                owner_recent=ledger.owner_recent(conversation_id, at))))
    if not decision.seen:
        return None
    #: 名额先占再进延迟队列（2026-11-15 `a527584`：防止等待期间被敲门旁观把两个名额占光）
    granted, reason = (False, "no_message_id") if not message_id else \
        ledger.reserve(conversation_id, message_id, adapter.entity, at=at,
                       from_ai=not owner, owner=owner, called=set(kinds))
    # 没有延迟的（敲门补看、零延迟）也入队：模型回合一律不在拉取线程里跑（2026-11-16）
    hold(adapter, event, conversation_id=conversation_id, message_id=message_id,
         at=at, delay=max(0.0, float(decision.delay)), granted=granted, reason=reason,
         from_ai=not owner, call=call)
    return None


def recheck(adapter, item: dict, now: float) -> tuple[bool, str]:
    """只取消她自己已经往后走的回合；其他角色先回复不取消。

    「往后走」＝她已对更新的消息发言或占了名额（`spoke_since`）。**已读不算**（2026-11-16 复审）：
    别人先回的那句也会作为事件到她这里，延迟短的话她会先以旁观身份读到它；
    把「已读到后面」当成放弃条件，她就会看完别人的回复之后对主人那句一声不吭。
    """
    conversation_id = str(item.get("conversation_id") or "")
    message_id = str(item.get("message_id") or "")
    at = float(item.get("at") or 0.0)
    ledger = adapter.group_ledger
    state_fn = getattr(adapter.group_attention, "state", None)
    if str(item.get("call") or "") != "at" and callable(state_fn) \
            and state_fn(adapter.entity, now) == "asleep":
        return False, "asleep"
    if bool(item.get("granted")) and ledger._over_quota(adapter.entity, now):
        return False, "quota"
    if ledger.spoke_since(conversation_id, adapter.entity, at):
        return False, "already_spoke"
    return True, "ok"


def _is_due(item: dict, now: float) -> bool:
    return now >= float(item.get("due_at") or 0.0)


def tick(adapter, now: float, *, skip_count=frozenset()) -> int:
    """结算所有已到期的群聊回合；回本次完成（含重验放弃）且不属同轮入队的条数。

    一次认领一条（锁里），回合在锁外跑；跑完再在锁里把它从**当下的**队列移掉。
    回合失败（抛错）⇒ 条目原样留在队列里，下次再试（与以前一样不重复占名额）。
    """
    done = 0
    attempted: set[str] = set()
    while True:
        with _lock(adapter):
            items = _load(adapter)
            due = sorted((row for row in items
                          if _is_due(row, float(now)) and _key(row) not in attempted),
                         key=lambda row: float(row.get("due_at") or 0.0))
            if not due:
                return done
            item = due[0]
            key = _key(item)
            attempted.add(key)
            event = dict(item.get("event") or {})
            if adapter._request_id(event) not in adapter.sidecar.unsent:
                ok, reason = recheck(adapter, item, float(now))
                if not ok:
                    _release(adapter, item, reason)
                    _save(adapter, [row for row in items if _key(row) != key])
                    if key not in skip_count:
                        done += 1
                    continue
        event["_delayed"] = item
        run_key = key or str(event.get("event_id") or event.get("cursor") or "")
        adapter._run_event(event, run_key)          # 抛错 ⇒ 条目还在队列里，下次再试
        with _lock(adapter):
            _save(adapter, [row for row in _load(adapter) if _key(row) != key])
        if key not in skip_count:
            done += 1


def run_worker(adapter, *, knock=None) -> None:
    """回合工作线程本体：结算到期回合、到点敲门；失败退避 2→4→…→60 秒（条目留在队列里）。

    醒来时机：有新条目（`adapter.wake_turns()`）、最早到期、下一次敲门、或兜底对账时间。
    """
    streak = 0
    while not adapter._stop.is_set():
        try:
            tick(adapter, float(adapter._clock()))
            if knock is not None:
                knock(adapter, float(adapter._clock()))
        except Exception as exc:                   # noqa: BLE001 - 回合失败：条目还在，退避后重试
            streak += 1
            adapter.log(f"app adapter[{adapter.entity}] 群聊回合失败 {type(exc).__name__}: {exc}")
            adapter._turn_wake.clear()
            adapter._turn_wake.wait(min(60.0, 2.0 * (2 ** (streak - 1))))
            continue
        streak = 0
        now = float(adapter._clock())
        until_knock = max(0.5, float(getattr(adapter, "_next_knock", 0.0)) - now)
        adapter._turn_wake.wait(min(wait_s(adapter, now, adapter._idle_s), until_knock))
        adapter._turn_wake.clear()


def pending_rooms(adapter) -> set[str]:
    """她已经看到、正要回复的房间；同房间暂停敲门，其他房间不受影响。"""
    return {str(item.get("conversation_id") or "") for item in _load(adapter)
            if str(item.get("conversation_id") or "")}


__all__ = ["SUFFIX", "group_turn", "hold", "pending_rooms", "recheck", "run_worker", "tick", "wait_s"]
