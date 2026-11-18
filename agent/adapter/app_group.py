"""App 三人房：每人自己「拿起手机」＋共享场账本（群聊自聊 M5a／M5b，`docs/GROUP-SELF-CHAT-20261108.md`）。

以前是「回应模型」：只有 owner 的消息能开一轮、一条消息只认一个 responder ⇒ 她们之间永远不互动。
现在是「环境模型」：

  · 任何人的新消息（含 AI）都是一次敲门；每个角色按 §5 的公式各自决定「这段之间我看到了吗」。
  · 看到的人可以申请发言名额（`GroupLedger.reserve`）：同一消息最多 2 人、同人 20 秒、
    一场 8 turn／300 秒、AI 互接 6 turn、每人每日 200／每小时 40 泡泡。
  · 拿到名额的人由大脑自己决定讲不讲；**沉默是合法结果，且多数时候应该是沉默**。
  · 没拿到名额、但看到了的人只旁观（进上下文、不回复）。

所有随机数都是「角色＋消息」的稳定散列，重试／重启不会把同一条消息的决定改掉；
第一次的决定还会记进账本，之后 `last_seen` 变了也照用原来那份。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime

#: 每小时平均拿起几次（aster 的先验值，GROUP-SELF-CHAT §3 B；2–6 点为 0）
GLANCE_PER_H = {0: 0.22, 1: 0.15, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0, 7: 0.5, 8: 0.9,
                9: 1.1, 10: 1.0, 11: 0.8, 12: 0.9, 13: 0.45, 14: 0.35, 15: 0.35, 16: 0.45,
                17: 0.55, 18: 0.65, 19: 0.8, 20: 0.85, 21: 0.75, 22: 0.5, 23: 0.25}
#: `@` 的拿起率随生物钟打折（owner 2026-11-08 §12-3）；心情系数由 `mood` 端口给（`brain/app/affect_wiring.py` 接上）
CALLED_P = {"awake": 0.95, "winding": 0.8, "waking": 0.8, "asleep": 0.3}
NAMED_P = 0.70                 # 句首叫名、没 @
NOTIFY_SCALE, NOTIFY_CAP = 0.3, 0.8   # 新消息的通知：0.3 × 当小时拿起率，上限 0.8
OWNER_FLOOR, OWNER_RECENT_S = 0.5, 1800.0   # owner 最后一句 30 分钟内，拿起率至少 0.5（不硬压）
BUSY_RANGE = (0.3, 2.5)        # 忙闲系数：按「日期＋人」稳定散列，对数均匀
#: 延迟中位（秒），对数正态 σ=0.5，截在 [0.3×, 3×]
DELAY_MEDIAN = {"active": 12.0, "called": 35.0, "named": 50.0, "notify": 90.0, "bystander": 45.0}

RESPONDERS_PER_MESSAGE = 2     # 含被 @ 的那位
SAME_SPEAKER_GAP_S = 20.0
SESSION_TURNS, SESSION_S = 8, 300.0       # owner 2026-11-08 §12-4（aster 原值 12）
AI_CHAIN_MAX, AI_CHAIN_RESET_S = 6, 360.0
BUBBLES_PER_DAY, BUBBLES_PER_HOUR = 200, 40
#: 定时敲门（§2 ①）：每人约 5 分钟一次、±150 秒抖动；没人说话时她才可能自己开场
KNOCK_S, KNOCK_JITTER_S = 300.0, 150.0
#: 房里最后一句离现在不到这么久就不算「安静」（话还在进行，不插一个新话题）
QUIET_S = 600.0
#: 同一个人两次「要不要开场」之间至少隔这么久（每次都要一次 LLM 调用；她多半会说不）
OPENER_TRY_GAP_S = 3600.0


def _unit(*parts: str) -> float:
    raw = "\0".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / float(1 << 64)


@dataclass(frozen=True, slots=True)
class AttentionDecision:
    seen: bool
    delay: float = 0.0
    kind: str = ""


class GroupAttention:
    """§5 的公式：`λ = GLANCE_PER_H[hour] × 忙闲`，`p = 1 - exp(-λ·dt)`，再叠三个修正。

    修正顺序固定：① `@`／叫名直接取值（跳过 λ 与忙闲）② 睡着归零（`@` 例外，但按生物钟打折）
    ③ owner 30 分钟内说过话 ⇒ 地板 0.5（睡着的人不会被地板叫醒）。新消息另有一次「通知」机会。
    """

    def __init__(self, circadian=None, mood=None) -> None:
        #: `{entity: CircadianClock}`
        self.circadian = circadian or {}
        #: 心情系数端口 `mood(entity, at) -> float`；没接时 1.0（2026-11-09 情感整理由 `affect_wiring.wire` 接上）
        self.mood = mood or (lambda _entity, _at: 1.0)

    def state(self, entity: str, at: float) -> str:
        clock = self.circadian.get(str(entity)) if self.circadian else None
        try:
            return str(clock.state(at)) if clock is not None else "awake"
        except Exception:
            return "awake"

    @staticmethod
    def busy_factor(entity: str, at: float) -> float:
        low, high = BUSY_RANGE
        day = datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d")
        return math.exp(math.log(low) + (math.log(high) - math.log(low)) * _unit("busy", entity, day))

    def rate(self, entity: str, at: float) -> float:
        """λ：这一小时平均拿起几次。"""
        return GLANCE_PER_H[datetime.fromtimestamp(float(at)).hour] * self.busy_factor(entity, at)

    def probability(self, entity: str, at: float, *, call: str = "", last_seen_at: float = 0.0,
                    owner_recent: bool = False, notify: bool = True) -> tuple[float, str]:
        """`notify=False`＝定时敲门：没有新消息的通知，只剩「这段之间自己拿起来」那一项。"""
        state = self.state(entity, at)
        if call == "at":
            mood = max(0.0, float(self.mood(entity, at)))
            return min(1.0, CALLED_P.get(state, CALLED_P["awake"]) * mood), "called"
        if state == "asleep":
            return 0.0, "asleep"
        if call == "named":
            return NAMED_P, "named"
        lam = self.rate(entity, at)
        hours = (float(at) - float(last_seen_at)) / 3600.0 if last_seen_at else 1.0
        glance = 1.0 - math.exp(-lam * max(0.0, min(hours, 24.0)))
        push = min(NOTIFY_CAP, NOTIFY_SCALE * lam) if notify else 0.0
        p = 1.0 - (1.0 - glance) * (1.0 - push)
        active = bool(last_seen_at) and float(at) - float(last_seen_at) < SESSION_S
        kind = "active" if active else "notify"
        if owner_recent:
            if p < OWNER_FLOOR:
                p = OWNER_FLOOR
            #: owner 刚说过话 ⇒ 这段对话是「现在进行中」，响应延迟按 active 算（中位 12 秒）。
            #: 不给这条，21 点后 owner 一句会拿到 notify 的 90 秒中位 ⇒ 实测相邻发言中位 410 秒（§10 要 7–15 秒）。
            kind = "active"
        return p, kind

    def decision(self, entity: str, message: dict, *, call: str = "", last_seen_at: float = 0.0,
                 owner_recent: bool = False) -> AttentionDecision:
        at = _at(message)
        p, kind = self.probability(entity, at, call=call, last_seen_at=last_seen_at,
                                   owner_recent=owner_recent)
        message_id = str(message.get("id") or message.get("created_at") or "")
        if _unit("pickup", entity, message_id) >= p:
            return AttentionDecision(seen=False, delay=0.0, kind=kind)
        return AttentionDecision(seen=True, delay=self.delay(entity, message_id, kind), kind=kind)

    @staticmethod
    def delay(entity: str, message_id: str, kind: str) -> float:
        median = DELAY_MEDIAN.get(kind, DELAY_MEDIAN["bystander"])
        first = max(1e-12, _unit("delay-a", entity, message_id))
        second = _unit("delay-b", entity, message_id)
        z = math.sqrt(-2.0 * math.log(first)) * math.cos(2.0 * math.pi * second)
        return max(0.3 * median, min(3.0 * median, median * math.exp(0.5 * z)))


def _at(message: dict) -> float:
    try:
        return float(message.get("created_at") or time.time())
    except (TypeError, ValueError):
        return time.time()


class GroupLedger:
    """三个角色共用的场账本（同一 brain 进程内；跨重启续用）。

    记的东西：每人上次真的看到房间的时刻、每条消息的拿起决定（重试照用）、每条消息的发言名额、
    场（开场时刻／turn 数）、AI 互接链、每人发言时刻与泡泡时间戳、owner 最后一句的时刻。
    名额在**开回合前**占（`reserve`），泡泡上限在**写库前**再判一次（`commit`）——
    中间隔着 40–60 秒的模型调用，第三个人不能拿旧账本过闸（aster 踩过）。
    """

    MAX_KEYS = 2000            # decisions／reservations 各保留最近这么多条

    def __init__(self, path, *, clock=time.time) -> None:
        self.path = pathlib.Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._data = self._load()

    # ── 落盘 ───────────────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self) -> None:
        for name in ("decisions", "reservations", "reservation_states"):
            rows = self._data.get(name) or {}
            for key in list(rows)[:max(0, len(rows) - self.MAX_KEYS)]:
                rows.pop(key, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)

    def _room(self, conversation_id: str) -> dict:
        rooms = self._data.setdefault("rooms", {})
        return rooms.setdefault(str(conversation_id), {})

    # ── 看见 ───────────────────────────────────────────────────────────────
    def note_message(self, conversation_id: str, *, owner: bool, at: float,
                     message_id: str = "") -> None:
        """owner 说话才更新 owner 时刻并把 AI 互接链归零；其他消息不写盘。"""
        if not owner:
            return
        with self._lock:
            room = self._room(conversation_id)
            if float(room.get("owner_at") or 0.0) >= at:
                return
            room["owner_at"] = at
            room["ai_chain"] = 0
            self._save()

    def spoke_since(self, conversation_id: str, entity: str, at: float) -> bool:
        """她是否已对 `at` 之后的消息发言或占了名额（延迟回合到期时「她自己已往后走」的唯一判据）。"""
        with self._lock:
            spoke = float((self._room(conversation_id).get("spoke") or {}).get(str(entity)) or 0.0)
        return spoke > at

    def rooms(self) -> list[str]:
        with self._lock:
            return list((self._data.get("rooms") or {}).keys())

    def remember_room(self, conversation_id: str, participants=()) -> None:
        """记住这个群与它的成员（定时敲门要知道去哪个房、回合的在场名册是谁）。"""
        people = sorted({str(who) for who in (participants or ()) if str(who)})
        with self._lock:
            known = str(conversation_id) in (self._data.get("rooms") or {})
            room = self._room(conversation_id)
            if known and (not people or room.get("participants") == people):
                return
            if people:
                room["participants"] = people
            self._save()

    def participants(self, conversation_id: str) -> list[str]:
        with self._lock:
            return list(self._room(conversation_id).get("participants") or [])

    def opener_due(self, conversation_id: str, entity: str, now: float) -> bool:
        """距她上一次「要不要开场」够久了吗（不论那次说没说）。"""
        with self._lock:
            tried = float((self._room(conversation_id).get("opener_try") or {}).get(str(entity)) or 0.0)
        return not tried or now - tried >= OPENER_TRY_GAP_S

    def note_opener_try(self, conversation_id: str, entity: str, now: float) -> None:
        with self._lock:
            self._room(conversation_id).setdefault("opener_try", {})[str(entity)] = now
            self._save()

    def owner_recent(self, conversation_id: str, at: float) -> bool:
        with self._lock:
            owner_at = float(self._room(conversation_id).get("owner_at") or 0.0)
        return bool(owner_at) and 0.0 <= at - owner_at < OWNER_RECENT_S

    def last_seen(self, conversation_id: str, entity: str) -> float:
        with self._lock:
            return float((self._room(conversation_id).get("seen") or {}).get(str(entity)) or 0.0)

    def mark_seen(self, conversation_id: str, entity: str, at: float) -> None:
        with self._lock:
            seen = self._room(conversation_id).setdefault("seen", {})
            if float(seen.get(str(entity)) or 0.0) < at:
                seen[str(entity)] = at
                self._save()

    def decision(self, conversation_id: str, message_id: str, entity: str, compute):
        """同一角色对同一消息只决定一次；重试／重启照用第一次的结果。"""
        key = "\0".join((str(conversation_id), str(message_id), str(entity)))
        with self._lock:
            stored = (self._data.get("decisions") or {}).get(key)
        if stored is not None:
            return AttentionDecision(bool(stored[0]), float(stored[1]), str(stored[2]))
        value = compute()
        with self._lock:
            self._data.setdefault("decisions", {})[key] = [value.seen, value.delay, value.kind]
            self._save()
        return value

    # ── 发言名额 ───────────────────────────────────────────────────────────
    def _bubbles(self, entity: str, now: float) -> list[float]:
        rows = [float(at) for at in (self._data.setdefault("bubbles", {}).get(str(entity)) or [])
                if now - float(at) < 86400.0]
        self._data["bubbles"][str(entity)] = rows
        return rows

    def _over_quota(self, entity: str, now: float, adding: int = 0) -> bool:
        rows = self._bubbles(entity, now)
        hour = sum(1 for at in rows if now - at < 3600.0)
        return len(rows) + adding > BUBBLES_PER_DAY or hour + adding > BUBBLES_PER_HOUR

    @staticmethod
    def _state_key(conversation_id: str, message_id: str, entity: str) -> str:
        return "\0".join((str(conversation_id), str(message_id), str(entity)))

    @staticmethod
    def _room_state(room: dict) -> dict:
        return {name: room.get(name) or 0
                for name in ("turn_no", "opened_at", "ai_chain", "last_turn_at")}

    def reservation_state(self, conversation_id: str, message_id: str, entity: str) -> dict:
        """给延迟条目保存可跨重启携带的占位快照。"""
        key = self._state_key(conversation_id, message_id, entity)
        with self._lock:
            return dict((self._data.get("reservation_states") or {}).get(key) or {})

    def reserve(self, conversation_id: str, message_id: str, entity: str, *, at: float,
                from_ai: bool, owner: bool, called=frozenset()) -> tuple[bool, str]:
        """占一个发言名额。成功即算一个 turn（会发起一次 LLM 调用）；同一角色重试幂等。"""
        entity = str(entity)
        key = "\0".join((str(conversation_id), str(message_id)))
        with self._lock:
            reservations = self._data.setdefault("reservations", {})
            taken = list(reservations.get(key) or [])
            if entity in taken:
                return True, "retry"
            # 被叫到的人各自保留一个名额；其余名额才轮得到没被叫的人（点名优先，但不硬压）
            called = {str(item) for item in (called or ())}
            others = max(0, RESPONDERS_PER_MESSAGE - len(called))
            if entity not in called and sum(who not in called for who in taken) >= others:
                return False, "message_full"
            if len(taken) >= RESPONDERS_PER_MESSAGE:
                return False, "message_full"
            room = self._room(conversation_id)
            spoke_rows = room.setdefault("spoke", {})
            before = self._room_state(room)
            spoke_before = {"present": entity in spoke_rows,
                            "value": float(spoke_rows.get(entity) or 0.0)}
            spoke = spoke_before["value"]
            if spoke and 0.0 <= at - spoke < SAME_SPEAKER_GAP_S:
                return False, "same_speaker_gap"
            last_turn = float(room.get("last_turn_at") or 0.0)
            if last_turn and at - last_turn >= AI_CHAIN_RESET_S:
                room["ai_chain"] = 0
            if from_ai and int(room.get("ai_chain") or 0) >= AI_CHAIN_MAX:
                return False, "ai_chain"
            opened = float(room.get("opened_at") or 0.0)
            expired = not opened or at - opened >= SESSION_S
            full = int(room.get("turn_no") or 0) >= SESSION_TURNS
            if expired or (full and owner):    # owner 开口就是一次新的敲门
                room["opened_at"], room["turn_no"] = at, 0
            elif full:
                return False, "session_full"
            if self._over_quota(entity, float(self._clock())):
                return False, "quota"
            room["turn_no"] = int(room.get("turn_no") or 0) + 1
            if from_ai:
                room["ai_chain"] = int(room.get("ai_chain") or 0) + 1
            room["last_turn_at"] = at
            spoke_rows[entity] = at
            reservations[key] = taken + [entity]
            self._data.setdefault("reservation_states", {})[
                self._state_key(conversation_id, message_id, entity)] = {
                    "before": before, "after": self._room_state(room), "spoke": spoke_before}
            self._save()
            return True, "ok"

    def release(self, conversation_id: str, message_id: str, entity: str, *,
                at: float | None = None, from_ai: bool = False, state: dict | None = None) -> bool:
        """释放预占名额；最近占位没有被后续回合覆盖时，精确恢复占位前的房间快照。"""
        entity = str(entity)
        key = "\0".join((str(conversation_id), str(message_id)))
        state_key = self._state_key(conversation_id, message_id, entity)
        with self._lock:
            reservations = self._data.setdefault("reservations", {})
            taken = list(reservations.get(key) or [])
            if entity not in taken:
                return False
            taken.remove(entity)
            if taken:
                reservations[key] = taken
            else:
                reservations.pop(key, None)
            stored = self._data.setdefault("reservation_states", {}).pop(state_key, {})
            snapshot = dict(state or stored)
            before, after = snapshot.get("before") or {}, snapshot.get("after") or {}
            room = self._room(conversation_id)
            current = self._room_state(room)
            if before and after and all(current.get(name) == after.get(name) for name in after):
                for name, value in before.items():
                    room[name] = value
            else:
                # 后续回合已经翻篇或 owner 已归零时不能恢复旧快照、抹掉别人的进展。
                if after and current.get("opened_at") == after.get("opened_at"):
                    room["turn_no"] = max(0, int(room.get("turn_no") or 0) - 1)
                if from_ai and float(room.get("owner_at") or 0.0) <= float(at or 0.0):
                    room["ai_chain"] = max(0, int(room.get("ai_chain") or 0) - 1)
            spoke = room.get("spoke") or {}
            if entity in spoke and (at is None or float(spoke.get(entity) or 0.0) <= float(at)):
                old = snapshot.get("spoke") or {}
                if old.get("present"):
                    spoke[entity] = float(old.get("value") or 0.0)
                else:
                    spoke.pop(entity, None)
            self._save()
            return True

    def commit(self, entity: str, bubbles: int) -> bool:
        """真正写库前再判一次泡泡上限；过了就记账，不过就整份不发。"""
        now = float(self._clock())
        with self._lock:
            if self._over_quota(str(entity), now, adding=max(1, int(bubbles))):
                return False
            self._bubbles(str(entity), now).extend([now] * max(1, int(bubbles)))
            self._save()
            return True


def knock_delay(entity: str, at: float) -> float:
    """下一次敲门的间隔：`KNOCK_S ± KNOCK_JITTER_S`，按「人＋时段」稳定散列（各人不同步）。"""
    return KNOCK_S + (2.0 * _unit("knock-jitter", entity, str(int(at // KNOCK_S))) - 1.0) * KNOCK_JITTER_S


def knock_roll(entity: str, conversation_id: str, at: float) -> float:
    return _unit("knock", entity, conversation_id, str(int(at)))


__all__ = ["AttentionDecision", "GroupAttention", "GroupLedger", "knock_delay", "knock_roll"]
