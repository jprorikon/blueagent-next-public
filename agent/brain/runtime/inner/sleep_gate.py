"""睡眠闸（owner 2026-11-16「睡眠时间还有内心活动和心情变化，不太合理」）＋让路逻辑。

以前心跳**完全没有睡眠判断**：只有「主人正在说话就让路」、预算和阈值。生物钟只管她自己排
活动的能力，心跳照样 30–150 秒一跳、独白照跑 ⇒ 实测睡窗内（1–6 点）每晚仍有三四十次独白
（间隔最短 2 分钟，内容还是「主人はまだ起きてる?」），心情定位也在花呼叫。

现在分三段（owner 选 A）：

  · `asleep`：**只做本地推进**——AEM 衰减、预算累积、生物钟、心跳留痕照走；
    不叫心情定位、不叫独白、不组句、不开口（`last_skip_reason = "asleep"`）。
  · `winding`／`waking`：照常，但独白间隔与心情定位的快取年龄**×2**（睡前醒后是过渡带）。
  · `awake`：与以前完全一致。

`force=True`（主人手动叫她开口）不受限。没有生物钟（测试／最小组合）一律当 `awake`。
"""

from __future__ import annotations

ASLEEP, WINDING, WAKING, AWAKE = "asleep", "winding", "waking", "awake"
TRANSITION_SCALE = 2.0


def from_clocks(clocks):
    """`{entity: CircadianClock}` → `state(entity, at) -> "asleep"|…`（没有就 `awake`）。"""
    table = dict(clocks or {})

    def state(entity: str, at: float) -> str:
        clock = table.get(str(entity))
        if clock is None:
            return AWAKE
        try:
            return str(clock.state(float(at)) or AWAKE).strip().lower()
        except Exception:                                  # noqa: BLE001 - 钟坏了当醒着
            return AWAKE

    return state


def state_of(beat, now: float) -> str:
    fn = getattr(beat, "sleep_state", None)
    if fn is None:
        return AWAKE
    try:
        value = str(fn(beat.entity, float(now)) or "").strip().lower()
    except Exception:                                      # noqa: BLE001
        return AWAKE
    return value if value in {ASLEEP, WINDING, WAKING, AWAKE} else AWAKE


def cadence_scale(beat, now: float) -> float:
    """`winding`／`waking` 的独白与心情定位降频系数（睡着不会走到这里）。"""
    return TRANSITION_SCALE if state_of(beat, now) in {WINDING, WAKING} else 1.0


def yield_to_owner(beat, now: float) -> bool:
    """主人正在跟她说话（或刚说完 `quiet_s` 秒内）⇒ 这一跳不开口、也不调模型。

    为什么要一路挡到 `quiet_s`：回合结束的那一刻他可能还要接着讲；心跳立刻插一句会抢发言权，
    也还是会撞到下一轮的推论 slot。
    """
    if beat.busy is None:
        return now < beat._yield_until
    try:
        talking = bool(beat.busy(beat.entity))
    except Exception:                                      # noqa: BLE001 - 问不到就当没人说话
        talking = False
    if talking:
        beat._yield_until = now + beat.quiet_s
        return True
    return now < beat._yield_until


def rest(beat, now: float) -> bool:
    """这一跳要不要「只做本地推进」。回 True 时已经写好 `skipped`／`last_skip_reason`。"""
    if yield_to_owner(beat, now):
        beat.skipped += 1
        beat.last_skip_reason = "owner_talking"
        return True
    state = state_of(beat, now)
    beat.last_sleep_state = state
    if state == ASLEEP:
        beat.skipped += 1
        beat.last_skip_reason = "asleep"
        return True
    return False


__all__ = ["ASLEEP", "AWAKE", "WAKING", "WINDING", "cadence_scale", "from_clocks", "rest",
           "state_of", "yield_to_owner"]
