"""主動消息的閘：**什麼時候她可以自己開口**（owner 2026-10-22「直接開工」的第 1 項）。

她做完一件公開的事（學到東西、玩完一段），也許想說一句。但「想說」不等於「該說」——
這個檔案就是那條界線，全部是**代碼判定**，不靠模型自我克制：

  ① **安靜時段**：預設 23:00–07:00 不主動（`quiet_hours`）。
  ② **最小間隔**：預設 45 分鐘內最多一次（同一個角色）。
  ③ **每日上限**：預設 6 次——防止「一天 40 條心情」。
  ④ **主人不在場就不發**：只看 `delivery` 的接收者（owner），不做「群發」。
  ⑤ **還沒回也可以再說，但不能重複**（common-ground 第 4 步；owner 2026-11-09 改定）：
     她上一條主動消息之後主人一句都沒說（任何端）⇒ `unanswered()` 記得那幾條；
     下一條跟它們是同一件事（`premise.repeats`：二字重疊佔較短那句 ≥0.5）⇒ `repeat_unanswered` 不發，
     組句與出口閘也都看得到「前の一言にまだ返事がない」，要她意識到已經發過。
     走 App Server 會話送出的還知道「讀了沒」（`note_delivered`／`note_read`／`delivery_state`）。
  ⑥ **主人不方便就不開口**（common-ground 第 8 步）：`interrupt(entity, principal, now)` 回理由
     （通話中／正在別處聊）⇒ 這一跳不說。信號在 `brain/app/interruptibility.py`。

它只決定「可不可以」，內容由呼叫端給；發送失敗不重試（下一次活動會再來）。
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import random
import threading
import time

from agent.brain.runtime.inner.clocks import politeness

#: 「還沒回」只看一天內的（再久的那條，主人回來時已經是另一件事了）
_SENT_WINDOW_S = 24 * 3600
#: 每個 (角色, 對象) 最多記幾筆送出時刻
_SENT_KEEP = 10

# ── 機率化搭話（owner 2026-11-11「按時間提升概率」）────────────────────────────
#: 時段權重（0–23 時，相對值）：白天她在做事，傍晚最想說話；夜裡由 `quiet_hours`／生物鐘先擋。
#: 這條曲線就是「按時段」的形狀——不再手寫「3 小時窗 ≤3 條」那種額度表。
PROACTIVE_CURVE = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.8, 1.0, 0.8,
                   0.6, 0.7, 0.9, 0.7, 0.6, 0.7, 0.9, 1.4, 1.6, 1.5,
                   1.3, 1.1, 0.8, 0.3)
#: 目標日均條數（owner 2026-11-11 定 20）⇒ 每小時基準率 = 目標 ÷ 曲線的日總和
PROACTIVE_DAILY_TARGET = 20.0
#: 每日上限的預設（安全閥；機率化之後仍要有一條硬上限防「反常的一天」）
PROACTIVE_DAILY_MAX = 26
#: 心情乘區：`0.5 + 0.9×social_need + 0.4×propensity`，夾在 [0.5, 1.8]
_MOOD_FLOOR, _MOOD_CEIL = 0.5, 1.8


def calibrate_rate(target: float, curve, min_gap_s: float) -> float:
    """反推「曲線權重＝1 那一小時」的基準率，讓**實際**日均條數落在 `target`。

    為什麼不能直接用 `target ÷ Σcurve`：每次開口之後有 `min_gap_s` 的不應期，那段時間不擲骰
    ⇒ 實際頻率是 `λ/(1+λR)` 而不是 λ（實測：目標 20 卻只來 14.5）。這裡用二分法把 R 算回去；
    `min_gap_s=0` 時就退化成 `target ÷ Σcurve`。
    """
    weights = [float(item) for item in (curve or PROACTIVE_CURVE)][:24]
    total_weight = sum(weights) or 1.0
    target = max(0.0, float(target))
    refractory_h = max(0.0, float(min_gap_s)) / 3600.0

    def observed(lam: float) -> float:
        return sum(lam * weight / (1.0 + lam * weight * refractory_h) for weight in weights)

    if refractory_h <= 0:
        return target / total_weight
    low, high = 0.0, max(1.0, target / total_weight) * 10.0 + 10.0
    for _ in range(80):
        middle = (low + high) / 2.0
        if observed(middle) < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0




class ProactiveGate:
    def __init__(self, *, min_gap_s: float = 20 * 60, daily_max: int = PROACTIVE_DAILY_MAX,
                 quiet_start: int = 23, quiet_end: int = 7,
                 users_dir=None, state_path=None, warmup_s: float = 0.0,
                 affect=None, clock=time.time, circadian=None, last_heard=None,
                 hazard: bool = False, daily_target: float | None = None,
                 rate_per_hour: float | None = None, curve=PROACTIVE_CURVE,
                 rng=None) -> None:
        self.min_gap_s = max(0.0, float(min_gap_s))
        self.daily_max = int(daily_max)          # 0＝一次都不准；負數＝無上限
        self.quiet_start = int(quiet_start)
        self.quiet_end = int(quiet_end)
        # ── 機率化（owner 2026-11-11）：到點之後**不是必發**，而是每跳按 p 擲一次骰子 ──
        #: 預設關（測試與舊行為都要確定性）；生產在 `boot_wiring` 依 env 打開。
        self.hazard = bool(hazard)
        self.curve = tuple(float(item) for item in (curve or PROACTIVE_CURVE))
        #: 每小時基準率（curve 權重＝1 的那一小時）。
        #: 只給 `daily_target` 就用 `calibrate_rate()` 反推（含不應期）——換時段形狀不必重算常數。
        if rate_per_hour is None:
            target = PROACTIVE_DAILY_TARGET if daily_target is None else float(daily_target)
            # 不應期（min_gap）那段不擲骰 ⇒ 由「實際日均」反推基準率，見 `calibrate_rate`
            rate_per_hour = calibrate_rate(target, self.curve, self.min_gap_s)
        self.rate_per_hour = max(0.0, float(rate_per_hour))
        self._rng = rng or random.random
        #: 每個角色最近一次的擲骰（`/api/health`／admin 看「為什麼這一跳沒開口」）
        self.last_hazard: dict[str, dict] = {}
        self.hazard_skips = 0
        #: 對方的時區從 `data/users/<principal>.md` 讀（沒有就退回她的時區）
        #: AEM 服務：決定「這次能不能用語音」（高喚醒＋心情正）
        self.affect = affect
        #: 各自的生物鐘（owner 2026-11-08）：`{entity: CircadianClock}`；睡着的角色不主動開口。
        self.circadian = circadian or {}
        #: 回寫 transcript 的回呼（`app/proactive_echo.make_proactive_recorder`）：
        #: 由 bootstrap 接上；沒接就只發不記（舊行為）。
        self.record: object = None
        self.users_dir = users_dir
        #: 閘門狀態落盤（`data/proactive.json`）：**重啟不該讓「20 分鐘間隔」清零**。
        #: 2026-10-22 實測：每次重啟她都會準時私聊一句，就是因為這兩個計數只活在記憶體裡。
        self._path = pathlib.Path(state_path) if state_path is not None else None
        #: 啟動暖機期（秒）：剛起來的這段時間不主動開口（避免「重啟＝她冒一句話」的機器人感）
        self.warmup_s = max(0.0, float(warmup_s))
        self._clock = clock
        self.started_at = float(clock())
        self._lock = threading.RLock()
        self._last: dict[str, float] = {}
        self._day: dict[tuple[str, str], int] = {}
        #: 最近說過的話（每個角色最多 5 條）——**同一句不要說兩次**
        self._recent: dict[str, list[str]] = {}
        #: `last_heard(entity, principal) -> float`：主人最後一次對她說話的時刻（任何端；0＝不知道）。
        #: 沒接也行：`note_heard()` 有被呼叫過（`TurnService` 每輪真的對話都會叫）就用落盤的那份。
        #: 兩者都沒有 ⇒ 不做「不疊著發」那條（舊行為）。
        self.last_heard = last_heard
        #: `interrupt(entity, principal, now) -> str`：主人此刻不方便的理由（空＝可以）；沒接 ⇒ 不看
        self.interrupt = None
        #: `entity|principal` → 主人最後一次對她說話的時刻（`note_heard`；落盤，重啟不丟）
        self._heard: dict[str, float] = {}
        #: 每個 (角色, 對象) 最近送出的時刻（`entity|principal` → [at…]，只留一天內 10 筆）
        self._sent: dict[str, list[float]] = {}
        #: 走 App Server 會話送出的那幾條（`entity|principal` → [{at, conversation_id, message_id, seq}]）
        #: ——有了它才知道「讀了沒」（PLAN-common-ground §7.4）；舊的 SSE 那條路沒有。
        self._delivered: dict[str, list[dict]] = {}
        #: 對方在那個會話讀到哪（`entity|principal|conversation_id` → message seq）
        self._read: dict[str, int] = {}
        self.sent = 0
        self.blocked = 0
        self._load()

    # ── 落盤 ────────────────────────────────────────────────────────────────
    def _load(self) -> None:
        if self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        last = raw.get("last")
        if isinstance(last, dict):
            for key, value in last.items():
                try:
                    self._last[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        recent = raw.get("recent")
        if isinstance(recent, dict):
            for key, rows in recent.items():
                if isinstance(rows, list):
                    self._recent[str(key)] = [str(row) for row in rows][-5:]
        delivered = raw.get("delivered")
        if isinstance(delivered, dict):
            for key, rows in delivered.items():
                if isinstance(rows, list):
                    self._delivered[str(key)] = [dict(row) for row in rows
                                                 if isinstance(row, dict)][-_SENT_KEEP:]
        read = raw.get("read")
        if isinstance(read, dict):
            for key, value in read.items():
                try:
                    self._read[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue
        heard = raw.get("heard")
        if isinstance(heard, dict):
            for key, value in heard.items():
                try:
                    self._heard[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        sent = raw.get("sent")
        if isinstance(sent, dict):
            for key, rows in sent.items():
                if isinstance(rows, list):
                    try:
                        self._sent[str(key)] = [float(row) for row in rows][-_SENT_KEEP:]
                    except (TypeError, ValueError):
                        continue
        day = raw.get("day")
        if isinstance(day, dict):
            for key, value in day.items():
                entity, _, date = str(key).partition("|")
                if not date:
                    continue
                try:
                    self._day[(entity, date)] = int(value)
                except (TypeError, ValueError):
                    continue

    def _save_locked(self) -> None:
        if self._path is None:
            return
        body = {"last": dict(self._last),
                "recent": {key: list(rows) for key, rows in self._recent.items()},
                "sent": {key: list(rows) for key, rows in self._sent.items()},
                "heard": dict(self._heard),
                "delivered": {key: list(rows) for key, rows in self._delivered.items()},
                "read": dict(self._read),
                "day": {f"{entity}|{date}": count
                        for (entity, date), count in self._day.items()},
                "saved_at": round(time.time(), 3)}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    # ── 判定 ────────────────────────────────────────────────────────────────
    def allows(self, entity: str, *, now: float | None = None,
               principal: str = "owner") -> tuple[bool, str]:
        at = float(now if now is not None else self._clock())
        # 暖機期：剛啟動就先安靜一會（她剛上線，不該立刻戳人）
        if self.warmup_s > 0 and (at - self.started_at) < self.warmup_s:
            return False, "warmup"
        # 她自己的作息（owner 2026-11-08）：睡着就不主動開口（主人的主動觸發走 force 另說）
        clock_of = self.circadian.get(str(entity or "")) if self.circadian else None
        if clock_of is not None and clock_of.asleep(at):
            self.last_window = {"quiet": False, "circadian_sleep": True}
            return False, "circadian_sleep"
        # 安靜時段：**她的時間**為主；知道對方時區就以對方為準（見 clocks.py）
        window = politeness(at, quiet_start=self.quiet_start, quiet_end=self.quiet_end,
                            entity=entity, principal=principal, users_dir=self.users_dir)
        self.last_window = window
        if window["quiet"]:
            return False, "quiet_hours"
        if self.interrupt is not None:
            try:
                busy = str(self.interrupt(str(entity or ""), str(principal or "owner"), at) or "")
            except Exception:                           # noqa: BLE001 - 信號壞了 ⇒ 當沒有
                busy = ""
            if busy:
                return False, busy
        key = str(entity or "")
        day = time.strftime("%Y-%m-%d", time.localtime(at))
        with self._lock:
            last = self._last.get(key, 0.0)
            if self.min_gap_s and (at - last) < self.min_gap_s:
                return False, "min_gap"
            if self.daily_max >= 0 and self._day.get((key, day), 0) >= self.daily_max:
                return False, "daily_max"
        return True, "ok"

    # ── 機率化：到點之後按「等了多久」與時段擲骰（owner 2026-11-11）──────────────
    #: 為什麼要這一層：以前 `allows()` 一放行就**必發**（實測 20 分鐘一條），
    #: 於是 aster／nori 2026-11-11 上午 11 點前就把一天 12 條用完。
    #: 改成危險率之後，長期頻率＝λ、等待越久越可能開口，時段形狀由 `curve` 表達。
    def mood_factor(self, mood: dict | None) -> float:
        """心情乘區（0.5–1.8）：越需要人、越坐不住 ⇒ 越容易開口。"""
        body = mood or {}
        try:
            need = float(body.get("social_need") or 0.0)
            propensity = float(body.get("propensity") or 0.0)
        except (TypeError, ValueError):
            need, propensity = 0.0, 0.0
        factor = 0.5 + 0.9 * min(1.0, max(0.0, need)) + 0.4 * min(1.0, max(0.0, propensity))
        return min(_MOOD_CEIL, max(_MOOD_FLOOR, factor))

    def hourly_rate(self, entity: str, *, now: float | None = None,
                    mood: dict | None = None) -> float:
        """這一刻的 λ（次／小時）＝基準率 × 時段權重 × 心情乘區。"""
        at = float(now if now is not None else self._clock())
        hour = int(time.localtime(at).tm_hour) % 24
        weight = self.curve[hour] if hour < len(self.curve) else 1.0
        return max(0.0, self.rate_per_hour * weight * self.mood_factor(mood))

    def hazard_p(self, entity: str, *, now: float | None = None, mood: dict | None = None,
                 dt: float = 60.0) -> float:
        """這一跳開口的機率：`1 − exp(−λ·dt)`（指數等待 ⇒ 等越久越可能，且不會超發）。"""
        if not self.hazard:
            return 1.0
        rate = self.hourly_rate(entity, now=now, mood=mood)
        span = max(0.0, float(dt)) / 3600.0
        return min(1.0, 1.0 - math.exp(-rate * span))

    def roll(self, entity: str, *, now: float | None = None, mood: dict | None = None,
             dt: float = 60.0) -> tuple[bool, dict]:
        """擲一次骰子；回 `(能不能開口, 這一次的帳)`。關掉機率化時恆回 True。"""
        at = float(now if now is not None else self._clock())
        p = self.hazard_p(entity, now=at, mood=mood, dt=dt)
        draw = 1.0 if not self.hazard else float(self._rng())
        ok = True if not self.hazard else draw < p
        info = {"at": round(at, 3), "p": round(p, 4), "roll": round(draw, 4), "ok": bool(ok),
                "rate": round(self.hourly_rate(entity, now=at, mood=mood), 3),
                "dt": round(float(dt), 1), "factor": round(self.mood_factor(mood), 3),
                "hour": int(time.localtime(at).tm_hour)}
        with self._lock:
            self.last_hazard[str(entity or "")] = info
            if not ok:
                self.hazard_skips += 1
        return ok, info

    # ── 還沒回的 ────────────────────────────────────────────────────────────
    def unanswered(self, entity: str, *, principal: str = "owner",
                   now: float | None = None) -> list[float]:
        """她送出之後、主人還沒再開口的那幾條（時刻，舊→新）。沒接 `last_heard` ⇒ 空。

        「還沒回」不等於「沒看到」：已讀不回也算在裡面——兩種情況都不該再疊一條。
        """
        pair = f"{entity}|{principal or 'owner'}"
        with self._lock:
            noted = self._heard.get(pair)
            rows = list(self._sent.get(pair, ()))
        if self.last_heard is None and noted is None:
            return []
        at = float(now if now is not None else self._clock())
        heard = float(noted or 0.0)
        if self.last_heard is not None:
            try:
                heard = max(heard, float(self.last_heard(str(entity or ""),
                                                         str(principal or "owner")) or 0.0))
            except Exception:                           # noqa: BLE001 - 問不到就只看落盤那份
                pass
        return [row for row in rows if row > heard and (at - row) <= _SENT_WINDOW_S]

    def note_delivered(self, entity: str, principal: str, *, conversation_id: str,
                       message_id: str = "", seq: int = 0, at: float | None = None) -> None:
        """這一條是經 App Server 會話送出的（通道回呼）：記下它在平台上的位置，之後才對得上已讀。"""
        if not str(conversation_id or "").strip():
            return
        stamp = float(at if at is not None else self._clock())
        pair = f"{entity}|{principal or 'owner'}"
        row = {"at": stamp, "conversation_id": str(conversation_id),
               "message_id": str(message_id or ""), "seq": int(seq or 0)}
        with self._lock:
            rows = [item for item in self._delivered.get(pair, [])
                    if (stamp - float(item.get("at") or 0.0)) <= _SENT_WINDOW_S]
            self._delivered[pair] = (rows + [row])[-_SENT_KEEP:]
            self._save_locked()

    def note_read(self, entity: str, principal: str, *, conversation_id: str, seq: int,
                  at: float | None = None) -> None:
        """對方在那個會話讀到第 `seq` 則（App Server 的 `chat.read.updated`）。只會往前走。"""
        key = f"{entity}|{principal or 'owner'}|{conversation_id}"
        with self._lock:
            if int(seq or 0) <= self._read.get(key, 0):
                return
            self._read[key] = int(seq or 0)
            self._save_locked()

    def delivery_state(self, entity: str, *, principal: str = "owner",
                       now: float | None = None) -> str:
        """她最後一條主動消息、還沒回的話，對方**讀了沒**：`unread`／`read`；回了或不知道 ⇒ 空字串。

        只有走 App Server 會話送出的才知道（舊的 SSE 路沒有已讀）。
        """
        pending = self.unanswered(entity, principal=principal, now=now)
        if not pending:
            return ""
        pair = f"{entity}|{principal or 'owner'}"
        with self._lock:
            rows = list(self._delivered.get(pair, ()))
            if not rows:
                return ""
            last = rows[-1]
            if abs(float(last.get("at") or 0.0) - pending[-1]) > 60.0:
                return ""                   # 最後那條不是走會話的 ⇒ 不知道
            read = self._read.get(f"{pair}|{last.get('conversation_id')}", 0)
        return "read" if int(last.get("seq") or 0) and int(last["seq"]) <= read else "unread"

    def stale_reads(self, entity: str, *, principal: str = "owner", now: float | None = None,
                    after_s: float | None = None) -> list[dict]:
        """讀了、`after_s`（預設 3 小時）還沒回的主動消息，**每條只交出一次**（交出即記號、落盤）。

        給心跳打 `ignored` 用（docs/PLAN-affect-reorg-20261109.md §3-6）。沒讀的不算（他沒看到）。
        """
        import os
        at = float(now if now is not None else self._clock())
        wait = float(after_s if after_s is not None
                     else os.environ.get("BLUEAGENT_AFFECT_IGNORED_S") or 3 * 3600)
        pending = self.unanswered(entity, principal=principal, now=at)
        if not pending:
            return []
        pair = f"{entity}|{principal or 'owner'}"
        out: list[dict] = []
        with self._lock:
            for row in self._delivered.get(pair, []):
                stamp = float(row.get("at") or 0.0)
                read = self._read.get(f"{pair}|{row.get('conversation_id')}", 0)
                if row.get("ignored") or not any(abs(stamp - item) <= 60.0 for item in pending):
                    continue
                if int(row.get("seq") or 0) and int(row["seq"]) <= read and at - stamp >= wait:
                    row["ignored"] = at
                    out.append(dict(row))
            if out:
                self._save_locked()
        return out

    def heard_at(self, entity: str, principal: str = "owner") -> float:
        """主人最後一次對她說話的時刻（`note_heard` 落盤的那份；不知道回 0）。"""
        with self._lock:
            return float(self._heard.get(f"{entity}|{principal or 'owner'}", 0.0))

    def note_heard(self, entity: str, principal: str, *, at: float | None = None) -> None:
        """主人（或客人）真的對她說了話 ⇒ 她之前的主動消息都算「回了」。"""
        if not str(entity or "").strip() or not str(principal or "").strip():
            return
        stamp = float(at if at is not None else self._clock())
        pair = f"{entity}|{principal}"
        with self._lock:
            if stamp <= self._heard.get(pair, 0.0):
                return
            self._heard[pair] = stamp
            self._save_locked()

    # ── 去重 ────────────────────────────────────────────────────────────────
    def recent_texts(self, entity: str, limit: int = 5) -> list[str]:
        with self._lock:
            return list(self._recent.get(str(entity or ""), []))[-max(1, int(limit)):]

    def _remember(self, entity: str, text: str) -> None:
        key = str(entity or "")
        with self._lock:
            rows = self._recent.setdefault(key, [])
            rows.append(str(text or "").strip())
            del rows[:-5]
            self._save_locked()

    def unanswered_texts(self, entity: str, *, principal: str = "owner",
                         now: float | None = None) -> list[str]:
        """還沒回的那幾條主動消息的原文（舊→新）；沒有就空。"""
        count = len(self.unanswered(entity, principal=principal, now=now))
        if not count:
            return []
        return self.recent_texts(entity, limit=5)[-count:]

    def _repeats_unanswered(self, entity: str, text: str, principal: str, now) -> bool:
        """跟還沒回的那幾條比是不是同一件事（換句話說也算）；判法與出口閘同一支（`premise.repeats`）。"""
        from agent.brain.runtime.inner.premise import repeats
        return repeats(text, self.unanswered_texts(entity, principal=principal, now=now))

    def _is_repeat(self, entity: str, text: str) -> bool:
        """跟最近 5 條比：二字重疊 ≥0.8 就算同一句（模型換句話說不算重複）。"""
        from agent.brain.memory.facts import grams
        wanted = grams(text)
        if not wanted:
            return False
        for prior in self.recent_texts(entity):
            other = grams(prior)
            if other and len(wanted & other) / max(1, len(wanted)) >= 0.8:
                return True
        return False

    def snapshot(self) -> dict:
        with self._lock:
            return {"sent": self.sent, "blocked": self.blocked,
                    "min_gap_s": self.min_gap_s, "daily_max": self.daily_max,
                    # 機率化搭話（2026-11-11）：現在這一跳的 λ／p 與是否被擲骰擋掉
                    "hazard": bool(self.hazard), "hazard_rate_per_hour": round(self.rate_per_hour, 3),
                    "hazard_skips": int(self.hazard_skips),
                    "hazard_last": {key: dict(row) for key, row in self.last_hazard.items()},
                    "quiet": f"{self.quiet_start:02d}:00-{self.quiet_end:02d}:00",
                    "warmup_s": self.warmup_s,
                    "last": {key: round(value, 1) for key, value in self._last.items()},
                    "recent": {key: list(rows) for key, rows in self._recent.items()},
                    "heard": {key: round(value, 1) for key, value in self._heard.items()},
                    # 走會話送出的最後一條在第幾則、對方讀到第幾則（整體驗收看這兩個）
                    "delivered": {key: rows[-1] for key, rows in self._delivered.items() if rows},
                    "read": dict(self._read)}

    def mark(self, entity: str, *, now: float | None = None, principal: str = "owner") -> None:
        at = float(now if now is not None else time.time())
        key = str(entity or "")
        day = time.strftime("%Y-%m-%d", time.localtime(at))
        with self._lock:
            self._last[key] = at
            self._day[(key, day)] = self._day.get((key, day), 0) + 1
            pair = f"{key}|{principal or 'owner'}"
            rows = [row for row in self._sent.get(pair, []) if (at - row) <= _SENT_WINDOW_S]
            self._sent[pair] = (rows + [at])[-_SENT_KEEP:]
            self.sent += 1
            self._save_locked()

    # ── 對外 ────────────────────────────────────────────────────────────────
    def maybe_send(self, entity: str, text: str, *, principal: str = "owner",
                   publish=None, now: float | None = None, urgency: str = "",
                   force: bool = False, dedupe: bool = True,
                   record=None) -> dict:
        """回 `{sent, reason}`。`publish(principal, frame)` 沒接就只是判定。

        `force=True`＝測試／主人手動觸發：**跳過暖機與間隔**（安靜時段仍然尊重，除非也指定
        `force=True` 且 `principal` 是主人——這條在 API 層已限定只有主人能叫）。

        `record(entity, principal, text, at)`：**真的發出去之後**把它寫回她的線
        （owner 2026-11-03：她主動說完，主人回信，她卻不知道自己說了什麼——因為那句
        只進了 outbox，沒進任何 transcript）。寫回失敗不影響發送。
        """
        body = str(text or "").strip()
        if not body:
            return {"sent": False, "reason": "empty"}
        if dedupe and self._is_repeat(entity, body):
            with self._lock:
                self.blocked += 1
            return {"sent": False, "reason": "repeat"}
        if dedupe and self._repeats_unanswered(entity, body, str(principal or "owner"), now):
            with self._lock:
                self.blocked += 1
            return {"sent": False, "reason": "repeat_unanswered"}
        if not force:
            ok, reason = self.allows(entity, now=now, principal=principal)
            if not ok:
                with self._lock:
                    self.blocked += 1
                return {"sent": False, "reason": reason}
        # 語音由 AEM 決定（高喚醒＋心情正才給）：只是「允許」，能不能真的出聲看通道。
        # AEM 從建構子帶進來（`ProactiveGuard(affect=...)`）；沒有就當「不給語音」。
        from agent.brain.runtime.expression import reason as voice_reason, voice_allowed
        affect_svc = getattr(self, "affect", None)
        mood_snap = {}
        if affect_svc is not None:
            try:
                mood_snap = affect_svc.mood(str(entity)) or {}
            except Exception:                           # noqa: BLE001
                mood_snap = {}
        want_voice = voice_allowed(mood_snap) if affect_svc is not None else False
        frame = {"kind": "proactive", "text": body, "entity": str(entity),
                 "want_voice": bool(want_voice),
                 "voice_reason": voice_reason(mood_snap) if affect_svc is not None else "no_affect",
                 "principal": str(principal),
                 "at": round(float(now if now is not None else time.time()), 3),
                 # `lonely` ⇒ 信道選擇會優先走語音（她想要人聲）
                 "urgency": str(urgency or "")}
        if publish is not None:
            try:
                publish(str(principal), frame)
            except Exception as exc:              # noqa: BLE001 - 發不出去不能炸活動
                return {"sent": False, "reason": f"publish_failed: {type(exc).__name__}"}
        self.mark(entity, now=now, principal=str(principal or "owner"))
        self._remember(entity, body)          # 同一句不要說第二次
        echo = record if record is not None else getattr(self, "record", None)
        if echo is not None:
            try:
                echo(str(entity), str(principal), body, float(frame.get("at") or 0.0))
            except Exception as exc:          # noqa: BLE001 - 記不上不能影響已送出的訊息
                print(f"proactive: 回寫 transcript 失敗（{type(exc).__name__}）", flush=True)
        return {"sent": True, "reason": "ok", "frame": frame}



__all__ = ["ProactiveGate"]
