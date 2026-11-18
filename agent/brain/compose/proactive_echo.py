"""把「她自己发出去的主动消息」写回她的 transcript（owner 2026-11-03）。

问题（实测）：她主动发了一句「成果物、見てくれる？」，主人回「見る」，
她回「何を見る？手元に何も出てないよ」——**她不知道自己在说什么**。
根因：`ProactiveGate.maybe_send()` 只把句子交给 `publish`（→ outbox／App／Discord），
从来没有写进任何 thread 的 transcript ⇒ 下一轮主人回话时，她的上下文里根本没有那句。

做法：`maybe_send(record=...)` 在真的发出去之后回调；这里实现「写回哪条线」——
挑**这个角色最近活跃、且主人在场**的那条线（主人下一句就会落在同一条线上），
用 `role="assistant"` 记一轮。没有这样的线时不新建（宁可不记，也不凭空造一条线）。
"""

from __future__ import annotations

from agent.brain.domain.thread import TurnRecord


def make_proactive_recorder(threads, *, log=None):
    """回 `record(entity, principal, text, at) -> bool`（给 `ProactiveGate` 用）。"""

    def record(entity: str, principal: str, text: str, at: float) -> bool:
        body = str(text or "").strip()
        who = str(entity or "").strip()
        if not body or not who:
            return False
        best = None
        try:
            rows = list(threads.threads())
        except Exception:                          # noqa: BLE001 - 记不上不能影响发送
            return False
        for thread in rows:
            key = getattr(thread, "key", None)
            if str(getattr(key, "entity", "")) != who:
                continue
            people = {str(item) for item in (getattr(thread, "participants", ()) or ())}
            if principal and str(principal) not in people:
                continue
            # 主動消息是**私下**發給這個人的 ⇒ 只寫進「只有她和他」的那條線。
            # 群聊線（他也在場、但還有別人）不算：寫進去的話下一輪群聊她會當著客人接著說，
            # 而他在私聊回信時那條線裡反而沒有這句（2026-11-03 review 實測挑中了群聊線）。
            if str(getattr(key, "scope", "") or "").startswith("group:"):
                continue
            if people - {str(principal or ""), who}:
                continue
            if best is None or float(getattr(thread, "last_active_at", 0) or 0) > \
                    float(getattr(best, "last_active_at", 0) or 0):
                best = thread
        if best is None:
            # 沒有任何「開著的」私聊線（過窗被 reap 掉／還沒開過）⇒ **開一條**再寫。
            # 以前這裡直接 return False：她的主動句就只存在 outbox/proactive.json，
            # 她自己那條線裡沒有 ⇒ 下一輪她根本不知道自己剛說過什麼
            # （owner 2026-11-04 實測：最近 10 條主動只落了 2 條）。
            try:
                from agent.brain.domain.event import SourceClass
                from agent.brain.domain.ids import ThreadKey
                # scope 要跟她平常那條線一致（主人＝"owner"、客人＝principal），
                # 否則開出來的是「另一條線」，她下一輪照樣看不到。
                scope = "owner" if str(principal or "") in {"", "owner"} else str(principal)
                key = ThreadKey(scope=scope, entity=who, scene="together", purpose="chat")
                attach = threads.attach(key, now=float(at), window=3600.0,
                                        lane=SourceClass.AMBIENT)
                best = attach.thread
            except Exception:                  # noqa: BLE001 - 開不了線也不能影響發送
                best = None
        if best is None:
            return False
        turn = TurnRecord(turn_id=f"t_proactive_{int(float(at) * 1000)}",
                          role="assistant", text=body, source="proactive",
                          at=float(at), speaker=who, addressed_to=str(principal or ""),
                          engage="proactive")
        try:
            with best.lock:
                best.record(turn)
                best.touch(float(at))
        except Exception:                          # noqa: BLE001
            return False
        if log is not None:
            try:
                log(f"proactive[{who}]: 寫回線 {getattr(best, 'id', '')}")
            except Exception:                      # noqa: BLE001
                pass
        return True

    return record
