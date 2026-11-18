"""各自的生物钟：她们各有各的作息（owner 2026-11-08）。

> 「我希望导入动态生物钟，让她们各自有各自的休息时间。」

三条设计：

1. **各自的作息型**：不是全局「23:00–07:00 一刀切」。每个角色有自己的入睡／起床基准点
   （ミラ 深夜型、アスター 早睡、ノリ 最早睡最早起），再叠加**每日抖动**（按日期稳定散列，
   同一天内不变——不然她会一小时变一次作息，看起来像坏了）。
2. **动态**：前一天若做到很晚（超过入睡点 1 小时还在活动），第二天整体后移（每晚最多 +20 分，
   累计上限 +90 分）；按时睡就慢慢回落。所以作息会随她的生活走，而不是永远一个点。
3. **状态分四段**：`awake`／`winding`（睡前 1 小时，只做收尾类）／`asleep`（不主动、不排新活动）／
   `waking`（醒后 30 分钟，慢慢回到正常）。私聊在 `asleep` 时先放着、醒了再读（2026-11-09，`adapter/app_dm_sleep.py`）；
   这套只约束**她自己**的节奏（自主动作、主动开口、群聊「拿起手机」）。

落盘：`data/circadian/<entity>.json`，只留最近 14 天（跨重启稳定）。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import threading
import time

#: 各角色的基准作息（分钟数，0–1439）。深夜型＝入睡点过了午夜。
BASE_SLEEP = {"mira": 2 * 60 + 10, "aster": 0 * 60 + 40, "nori": 23 * 60 + 50}
BASE_WAKE = {"mira": 9 * 60 + 0, "aster": 7 * 60 + 30, "nori": 7 * 60 + 0}
#: 没有登记的角色用这个
DEFAULT_BASE = (1 * 60 + 30, 8 * 60 + 30)

#: 每日抖动的幅度（分钟）：±40 ⇒ 同一个人每天不完全一样，但一天内固定
DAILY_JITTER = 40
#: 睡前／醒后的过渡带（分钟）
WINDING_MIN = 60
WAKING_MIN = 30
#: 前一天晚睡的代价：每晚最多后移多少、累计上限多少（分钟）
LATE_SHIFT_STEP = 20
MAX_SHIFT = 90
#: 判定「昨晚睡晚了」：活动结束比入睡点还晚这么多分钟
LATE_TOLERANCE = 60


class CircadianClock:
    """一个角色的作息钟（可持久化，跨重启稳定）。"""

    def __init__(self, entity: str, *, state_dir=None, clock=time.time) -> None:
        self.entity = str(entity)
        self.clock = clock
        self.path = pathlib.Path(state_dir) / f"{self.entity}.json" if state_dir else None
        #: 同一個鐘被 LifeLoop、主動開口、App adapter 幾條執行緒共用；釘窗／回落／後移都要串行，
        #: 否則兩條線同時寫同一個 .tmp ⇒ os.replace 找不到檔、在 LifeLoop 裡炸開（REVIEW 2026-11-08）。
        self._lock = threading.RLock()
        self._state = self._load()

    # ── 落盘 ───────────────────────────────────────────────────────────────────
    def _load(self) -> dict:
        payload: dict = {}
        if self.path is not None:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        days = payload.get("days") if isinstance(payload.get("days"), dict) else {}
        return {"shift_min": int(payload.get("shift_min") or 0),
                "settled_day": str(payload.get("settled_day") or ""),
                "days": {str(key): dict(value) for key, value in days.items()
                         if isinstance(value, dict)}}

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 只留最近 14 天
        days = dict(sorted(self._state["days"].items())[-14:])
        self._state["days"] = days
        temporary = self.path.with_name(f".{self.path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, sort_keys=True),
                             encoding="utf-8")
        os.replace(temporary, self.path)

    # ── 作息 ───────────────────────────────────────────────────────────────────
    def _jitter(self, day_key: str) -> int:
        """当天抖动：按「角色＋日期」稳定散列，同一天内不变。"""
        digest = hashlib.blake2s(f"{self.entity}|{day_key}".encode("utf-8"), digest_size=4)
        return int.from_bytes(digest.digest(), "big") % (DAILY_JITTER * 2 + 1) - DAILY_JITTER

    def window(self, now: float | None = None) -> dict:
        with self._lock:
            return self._window_locked(now)

    def _window_locked(self, now: float | None = None) -> dict:
        """这一夜的作息窗：`{day, sleep_min, wake_min, shift_min, sleep_at, wake_at}`。

        睡前那次调用就把当天的窗口钉住（写进 `days`），当天不再变。
        """
        at = float(now if now is not None else self.clock())
        moment = time.localtime(at)
        day_key = time.strftime("%Y-%m-%d", moment)
        base_sleep = BASE_SLEEP.get(self.entity, DEFAULT_BASE[0])
        base_wake = BASE_WAKE.get(self.entity, DEFAULT_BASE[1])
        pinned = self._state["days"].get(day_key)
        if pinned and "sleep_min" in pinned and "wake_min" in pinned:
            sleep_min = int(pinned["sleep_min"])
            wake_min = int(pinned["wake_min"])
        else:
            shift = max(-MAX_SHIFT, min(MAX_SHIFT, int(self._state.get("shift_min") or 0)))
            jitter = self._jitter(day_key)
            sleep_min = (base_sleep + shift + jitter) % (24 * 60)
            wake_min = (base_wake + shift // 2 + jitter // 2) % (24 * 60)
            self._state["days"][day_key] = {"sleep_min": sleep_min,
                                            "wake_min": wake_min,
                                            "shift_min": shift}
            self._save()
        midnight = time.mktime((moment.tm_year, moment.tm_mon, moment.tm_mday,
                                0, 0, 0, 0, 0, -1))
        # 「这一天的那一夜」＝傍晚开始睡的那一觉：入睡点过了午夜（<12:00）就算到第二天凌晨。
        sleep_at = midnight + sleep_min * 60.0
        wake_at = midnight + wake_min * 60.0
        if sleep_min < 12 * 60:                  # 深夜型（00:40／02:10）⇒ 这一觉在 D+1 凌晨
            sleep_at += 24 * 3600.0
            wake_at += 24 * 3600.0
        while wake_at <= sleep_at:               # 起床点早于入睡点（跨午夜）⇒ 顺延一天
            wake_at += 24 * 3600.0
        return {"day": day_key, "sleep_min": sleep_min, "wake_min": wake_min,
                "shift_min": int(self._state.get("shift_min") or 0),
                "sleep_at": sleep_at, "wake_at": wake_at}

    def state(self, now: float | None = None) -> str:
        """`awake`／`winding`（睡前 1 小时）／`asleep`／`waking`（醒后 30 分钟）。"""
        at = float(now if now is not None else self.clock())
        # 先看「昨晚的窗」和「今天的窗」——入睡点可能在今天凌晨或今晚
        # REVIEW 2026-11-08 A8：`waking` 是**醒來之後**的 30 分鐘（以前算成醒前 30 分鐘，
        # 等於提早半小時把她當成醒著）；整個 [sleep_at, wake_at) 都是 asleep。
        for window in self._candidate_windows(at):
            if window["sleep_at"] <= at < window["wake_at"]:
                return "asleep"
        for window in self._candidate_windows(at):
            if window["wake_at"] <= at < window["wake_at"] + WAKING_MIN * 60.0:
                return "waking"
            if window["sleep_at"] - WINDING_MIN * 60.0 <= at < window["sleep_at"]:
                return "winding"
        return "awake"

    def settling_in(self, now: float | None = None) -> bool:
        """睡前一小時／醒後半小時：做完手上這件可以，**不開新的一件**（只做收尾）。"""
        return self.state(now) in {"winding", "waking"}

    def sleep_spans(self, days: int = 14) -> list[tuple[float, float]]:
        """最近 N 天的睡窗 `[(sleep_at, wake_at)]`——给 AEM 衰减换速用（`inner/sleep_decay.py`）。"""
        spans: list[tuple[float, float]] = []
        for offset in range(max(1, int(days))):
            moment = time.localtime(float(self.clock()) - offset * 86400.0)
            day_at = time.mktime((moment.tm_year, moment.tm_mon, moment.tm_mday,
                                  12, 0, 0, 0, 0, -1))
            try:
                window = self.window(day_at)
                spans.append((float(window["sleep_at"]), float(window["wake_at"])))
            except Exception:                              # noqa: BLE001 - 算不出来就跳过这一天
                continue
        return sorted(set(spans))

    def asleep(self, now: float | None = None) -> bool:
        return self.state(now) == "asleep"

    def night_window(self, at: float | None = None) -> dict:
        """「当前这一夜」的窗口：凌晨（<12:00）算前一天傍晚开始的那一觉。"""
        moment = time.localtime(float(at if at is not None else self.clock()))
        anchor = float(at if at is not None else self.clock())
        if moment.tm_hour < 12:                   # 凌晨 ⇒ 昨晚那觉
            anchor -= 24 * 3600.0
        day = time.localtime(anchor)
        noon = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 12, 0, 0, 0, 0, -1))
        return self.window(noon)

    def _candidate_windows(self, at: float) -> list[dict]:
        """当前时刻可能落在的两个窗：前一天的（入睡在昨天、起床到今天）＋今天的。"""
        out = []
        for offset_days in (-1, 0):
            moment = time.localtime(at + offset_days * 86400.0)
            day_at = time.mktime((moment.tm_year, moment.tm_mon, moment.tm_mday,
                                  12, 0, 0, 0, 0, -1))
            window = self.window(day_at)
            out.append(window)
        return out

    # ── 动态：晚睡 → 第二天后移 ────────────────────────────────────────────────
    def note_activity_end(self, ended_at: float | None = None) -> int:
        with self._lock:
            return self._note_activity_end_locked(ended_at)

    def _note_activity_end_locked(self, ended_at: float | None = None) -> int:
        """记一次活动结束；如果她熬过了入睡点，第二天作息后移（回新的 shift）。"""
        at = float(ended_at if ended_at is not None else self.clock())
        window = self.night_window(at)
        # 「熬夜」＝ 这件事在她自己的作息窗里才收尾（还没睡就先躺过去了）。
        if not (window["sleep_at"] <= at < window["wake_at"]):
            return int(self._state.get("shift_min") or 0)
        if (at - window["sleep_at"]) / 60.0 < LATE_TOLERANCE:
            return int(self._state.get("shift_min") or 0)
        shift = max(-MAX_SHIFT, min(MAX_SHIFT,
                                    int(self._state.get("shift_min") or 0) + LATE_SHIFT_STEP))
        self._state["shift_min"] = shift
        self._save()
        return shift

    def settle(self, now: float | None = None) -> int:
        with self._lock:
            return self._settle_locked(now)

    def _settle_locked(self, now: float | None = None) -> int:
        """按时睡的人慢慢回落（**每天**回 5 分钟），别让一次熬夜永久改变作息。

        REVIEW 2026-11-08 A6：以前没有任何地方呼叫 ⇒ 作息只会往后推、永不回落；
        現在由 LifeLoop 在她醒著時呼叫，這裡用 `settled_day` 保證一天只回一次（不然每一跳都減 5）。
        """
        at = float(now if now is not None else self.clock())
        window = self.night_window(at)
        today = time.strftime("%Y-%m-%d", time.localtime(at))
        if self._state.get("settled_day") == today:
            return int(self._state.get("shift_min") or 0)
        if at < window["sleep_at"] and int(self._state.get("shift_min") or 0) > 0:
            self._state["shift_min"] = max(0, int(self._state["shift_min"]) - 5)
            self._state["settled_day"] = today
            self._save()
        return int(self._state.get("shift_min") or 0)

    # ── 给人看的快照 ───────────────────────────────────────────────────────────
    def snapshot(self, now: float | None = None) -> dict:
        at = float(now if now is not None else self.clock())
        window = self.night_window(at)
        return {"entity": self.entity, "state": self.state(at),
                "sleep": _hhmm(window["sleep_min"]), "wake": _hhmm(window["wake_min"]),
                "shift_min": window["shift_min"], "day": window["day"]}


def _hhmm(minutes: int) -> str:
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def build_clocks_from_env(base, entities, *, clock=time.time) -> dict[str, CircadianClock]:
    """按环境变量建钟：`BLUEAGENT_CIRCADIAN=0` 时整条关掉（空 dict）。"""
    if str(os.environ.get("BLUEAGENT_CIRCADIAN") or "1").strip().lower() \
            in {"0", "off", "false", "no"}:
        return {}
    return build_clocks(entities, pathlib.Path(base) / "circadian", clock=clock)


def build_clocks(entities, state_dir, *, clock=time.time) -> dict[str, CircadianClock]:
    """按角色库建钟（每个实体一份，落 `data/circadian/<id>.json`）。"""
    ids = [entity.id for entity in (entities.all() if entities is not None else [])]
    return {entity_id: CircadianClock(entity_id, state_dir=state_dir, clock=clock)
            for entity_id in ids}


__all__ = ["BASE_SLEEP", "BASE_WAKE", "CircadianClock", "build_clocks",
           "build_clocks_from_env"]
