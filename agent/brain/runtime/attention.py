"""AttentionGate：確定性特徵 → 四級動作；租約持久化；shadow 記錄。

不變量：
  · 明確 @/reply/喚醒 → REPLY，跳過評分；
  · 明確對另一個 principal 說 → SILENT；
  · 同一個 A 的追問 → FocusLease → REPLY；
  · 其餘用 P/V/R/C 打分，落在不同區間給 REACT/YIELD/SILENT；
  · 每條訊息都記 human 速率，每次回覆都記 agent 頻率（C_intrusion）。
"""

from __future__ import annotations

import threading
import time
from collections import deque

from agent.brain.domain.attention import (AttentionConfig, AttentionDecision,
                                             AttentionFeatures, FocusLease)
from agent.brain.domain.decision import Engagement
from agent.brain.domain.event import IngressEvent, InputKind, SourceClass
from agent.brain.domain.thread import Thread
from agent.brain.runtime.attention_history import AttentionHistory
from agent.brain.runtime.lease_store import FocusLeaseStore

_TERMINATORS = ("谢谢", "謝謝", "明白了", "好的", "收到", "ok", "OK")
_MEMO_WORDS = ("备忘", "備忘", "待办", "待辦", "提醒", "安排", "上线", "上線",
               "记得", "記得", "日程", "会议", "會議", "对齐", "對齊", "deadline",
               "todo", "schedule")
_QUESTION_WORDS = ("有人知道", "有人會", "有人会", "谁知道", "誰知道", "有没有人",
                    "有沒有", "怎么", "怎麼", "为什么", "為什麼", "多少", "哪个",
                    "哪個", "是不是", "吗", "嗎")


class IntrusionMeter:
    def __init__(self) -> None:
        self._agent: dict[str, deque[float]] = {}
        self._human: dict[str, deque[float]] = {}

    def note_human(self, thread_id: str, now: float) -> None:
        bucket = self._human.setdefault(thread_id, deque())
        bucket.append(now)
        while bucket and now - bucket[0] > 300:
            bucket.popleft()

    def note_agent(self, entity: str, now: float) -> None:
        bucket = self._agent.setdefault(entity, deque())
        bucket.append(now)
        while bucket and now - bucket[0] > 300:
            bucket.popleft()

    def intrusion(self, thread_id: str, entity: str, now: float) -> float:
        agent = self._agent.get(entity, deque())
        human = self._human.get(thread_id, deque())
        recent_agent = sum(1 for at in agent if now - at <= 300)
        recent_human = sum(1 for at in human if now - at <= 60)
        return min(1.0, recent_agent / 3 * 0.6 + min(1.0, recent_human / 20) * 0.4)

    def snapshot(self, entity: str, thread_id: str, now: float) -> dict:
        return {"intrusion": round(self.intrusion(thread_id, entity, now), 3),
                "agent_recent_5m": sum(1 for at in self._agent.get(entity, deque())
                                       if now - at <= 300)}


class AttentionGate:
    def __init__(self, config: AttentionConfig | None = None, *,
                 lease_path=None, log_path=None) -> None:
        self.config = config or AttentionConfig()
        self.leases = FocusLeaseStore(lease_path)
        self.meter = IntrusionMeter()
        self.history = AttentionHistory(log_path)
        self._lock = threading.RLock()
        self._recent: deque[dict] = deque(maxlen=50)
        self._counts: dict[str, int] = {}

    def note_human(self, event: IngressEvent, thread: Thread, now: float) -> None:
        self.meter.note_human(thread.id, now)

    def note_agent(self, decision, event: IngressEvent, now: float) -> None:
        self.meter.note_agent(event.entity, now)
        if decision.engage is Engagement.REPLY and event.scope.startswith("group:"):
            self.leases.grant(entity=event.entity, thread_id=decision.thread_id,
                              principal=event.principal, group=event.scope, now=now,
                              config=self.config)

    def decide(self, event: IngressEvent, thread: Thread, base: Engagement, *,
               recall_hits: int = 0, reactions: bool | None = None,
               now: float | None = None) -> AttentionDecision:
        at = float(now if now is not None else time.time())
        can_react = event.reactions if reactions is None else bool(reactions)

        def finish(proposed: Engagement, score: float, reason: str, *,
                   lease_id: str = "", yield_ms: int = 0) -> AttentionDecision:
            return self._finish(event, thread, base, proposed, score, reason,
                                lease_id=lease_id, yield_ms=yield_ms, now=at)

        if event.payload.get("force_reply"):
            return finish(Engagement.REPLY, 1.0, "forced")
        if base is Engagement.IGNORE:
            return finish(Engagement.IGNORE, 0.0, "explicit")
        if base is Engagement.REPLY:
            return finish(Engagement.REPLY, 1.0, "explicit")
        target = str(event.addressed_to or "").strip().lower()
        # 收件人**查過了、不是她**（Discord 群聊：沒 @／沒引用她／沒叫名字／也不是延續）。
        # 以前這裡會落到下面的 FocusLease 與評分 ⇒ 她自己先前被叫到過就會一直回，
        # 三顆 bot 各持一份租約 ⇒ 一句「今日何をしました？」三顆都答（owner 2026-10-27）。
        # 空字串（`""`＝呼叫端根本沒算過）仍走舊路，單人／App 的追問不受影響。
        if target in {"unknown", "未知"}:
            self.leases.revoke(thread.id, event.principal)
            return finish(Engagement.SILENT, 0.0, "addressee_unknown")
        if target and target not in {"entity", "group", "any"}:
            self.leases.revoke(thread.id)
            return finish(Engagement.SILENT, 0.0, f"to_other:{target}")
        if any(word in event.text for word in _TERMINATORS):
            self.leases.revoke(thread.id, event.principal)
            return finish(Engagement.SILENT, 0.0, "terminator")
        lease = self.leases.match(thread.id, event.principal, at)
        if lease is not None:
            self.leases.consume(lease)
            return finish(Engagement.REPLY, 1.0, "focus_lease", lease_id=lease.thread_id)

        features = self._features(event, thread, recall_hits, at)
        score = features.score(self.config)
        if score >= self.config.reply_threshold:
            return finish(Engagement.REPLY, score, "score:reply")
        if score >= self.config.yield_threshold:
            yield_ms = self.config.voice_yield_ms if (
                event.source_class is SourceClass.REALTIME
                or event.kind is InputKind.AUDIO) else self.config.yield_ms
            return finish(Engagement.YIELD, score, "score:yield", yield_ms=yield_ms)
        if score >= self.config.react_threshold and features.memo and can_react:
            return finish(Engagement.REACT, score, "score:react")
        return finish(Engagement.SILENT, score, "score:silent")

    def _features(self, event: IngressEvent, thread: Thread,
                  recall_hits: int, now: float) -> AttentionFeatures:
        target = str(event.addressed_to or "").strip().lower()
        text = event.text
        if target == "group":
            p_target = 0.90
        elif any(word in text for word in _QUESTION_WORDS):
            p_target = 0.80
        elif thread.transcript and thread.transcript[-1].speaker == event.principal:
            p_target = 0.40
        else:
            p_target = 0.25
        memo = any(word in text for word in _MEMO_WORDS)
        role = 0.85 if memo else 0.20
        value = min(1.0, max(0, recall_hits) / 2)
        intrusion = self.meter.intrusion(thread.id, event.entity, now)
        return AttentionFeatures(target=p_target, value=value, role=role,
                                 intrusion=intrusion, memo=memo)

    def _finish(self, event: IngressEvent, thread: Thread, actual: Engagement,
                proposed: Engagement, score: float, reason: str, *,
                lease_id: str = "", yield_ms: int = 0,
                now: float) -> AttentionDecision:
        shadow = self.config.shadow
        action = proposed if not shadow else (
            Engagement.REPLY if actual is Engagement.REPLY else Engagement.OBSERVE)
        final = AttentionDecision(action=action, score=round(score, 4),
                                  reason=(f"shadow:{reason}" if shadow else reason),
                                  lease_id=lease_id,
                                  yield_ms=(yield_ms if action is Engagement.YIELD else 0))
        row = {"at": round(now, 3), "entity": event.entity,
               "thread": thread.id, "principal": event.principal,
               "source": event.source, "scope": event.scope,
               "proposed": proposed.value,
               "actual": final.action.value, "score": round(score, 4),
               "reason": reason, "shadow": shadow}
        with self._lock:
            self._recent.append(row)
            self._counts[final.action.value] = self._counts.get(final.action.value, 0) + 1
        self.history.record(row)
        return final

    def snapshot(self, entity: str, thread_id: str, now: float) -> dict:
        config = self.config
        with self._lock:
            recent = list(self._recent)[-10:]
            counts = dict(self._counts)
        return {"mode": "shadow" if config.shadow else "active",
                "config": {"lease_ttl_s": config.lease_ttl_s,
                           "lease_turns": config.lease_turns,
                           "yield_ms": config.yield_ms,
                           "reply_threshold": config.reply_threshold,
                           "yield_threshold": config.yield_threshold,
                           "react_threshold": config.react_threshold},
                "leases": self.leases.snapshot(now),
                "meter": self.meter.snapshot(entity, thread_id, now),
                "metrics": {"counts": counts,
                            "total": sum(counts.values())},
                "history": self.history.summary(),
                "recent": recent}
