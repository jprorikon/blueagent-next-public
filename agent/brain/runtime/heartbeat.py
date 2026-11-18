"""心跳：**每個角色一條，各自獨立**（owner 2026-10-22）。

為什麼不能共用一條（前一版就是一條迴圈掃三個實體）：

  · 節拍不同：ミラ 話多、アスター 慢、ノリ 更慢——同一個 interval 等於把三個人綁在一起。
  · 狀態不同：一個人正在說話，不該拖住另一個人的心跳；一個人的冷卻也不該讓另一個人閉嘴。
  · 觀測不同：`/api/state` 要能分別看到「誰的心跳、跳了幾次、說了幾次」。

每次跳動仍只做三件事（與前一版相同）：

  ① `affect.advance(entity)`：推進情緒（衰減／重算，不是刺激）
  ② 算 `propensity`（心情＋被喚起＋寂寞）
  ③ 過 `ProactiveGate` 才推一句；寂寞度高 ⇒ `urgency="lonely"`（信道選擇會先試語音）

紀律：
  · **安靜時段照跳**，只是不出聲（`ticks` 照增、狀態照推進）。
  · 失敗只記 log，不重試、不影響下一跳。
  · 一條心跳只管一個角色——`entity` 是建構參數，不是迴圈變數。
"""

from __future__ import annotations

import os
import threading
import time

# 沒有 compose（模型）時的模板台詞在 `runtime/inner/mood_words.py`（情感整理：語氣詞表收在一處）
from agent.brain.runtime.inner import drive, self_facts, sleep_gate
from agent.brain.runtime.inner.mood_words import TEMPLATES as _TEMPLATES, template as _template  # noqa: F401


#: 模板台詞最多塞幾筆到期預約（多的留到下一次開口，不會被標掉）
_TEMPLATE_DUE_MAX = 3


class Heartbeat:
    """**一個角色**的心跳。要三個角色就建三個（見 `build_heartbeats`）。"""

    def __init__(self, *, entity: str, affect, gate, publish=None, compose=None,
                 interval_s: float = 60.0, threshold: float = 0.35,
                 activity_log=None, log=None, clock=time.time,
                 principal: str = "owner", budget=None, reminders=None, beats=None,
                 moodline=None, monologue=None, on_self_work=None,
                 drive_base=None, on_drive_work=None, drive_gap_s: float = 2700.0,
                 drive_daily_cap: int = 4, drive_running=None,
                 world=None,
                 busy=None, quiet_s: float = 8.0, recent_talk=None, sleep_state=None,
                 talk_route: str | None = None, premise=None, visible=None) -> None:
        self.entity = str(entity)
        self.affect = affect
        self.gate = gate
        self.publish = publish
        self.compose = compose
        self.interval_s = max(5.0, float(interval_s))
        self.threshold = float(threshold)
        #: 基準值（arousal 調制前的原始間隔／門檻）；`interval_s`/`threshold` 是「這一跳的有效值」
        self.base_interval_s = self.interval_s
        self.base_threshold = self.threshold
        #: 由 arousal 調制：間隔 0.5×–2.5×、門檻 0.30–0.50（owner 2026-10-22 第③條）
        self.arousal_pacing = True
        self.last_effective = {}
        #: 精力剎車：預算低於 `rest_below` ⇒ 自動降頻＋更難開口（她累了）
        self.energy_brake = True
        self.rest_below = 3.0
        self.activity_log = activity_log
        self._log = log or (lambda _message: None)
        self._clock = clock
        self.principal = str(principal or "owner")
        #: 行動預算（**心跳累積**；自主行動消耗；他人發起的不受限）
        self.budget = budget
        #: 「幾跳之後要提某件事」的預約（`runtime/inner/reminders.py`）；每跳取出到期的。
        self.reminders = reminders
        #: 心跳留痕（`runtime/inner/beats.py`）：每一跳寫一行，事後能回指「第 N 跳」。
        self.beats = beats
        #: 心情定位（`runtime/inner/moodline.py`）：把 v／a 翻成一句人話。
        #: 自己判要不要花呼叫（三條快取條件），所以每跳都叫也不會亂燒（owner 2026-11-01）。
        self.moodline = moodline
        #: 內心獨白（`runtime/inner/monologue.py`）：每幾分鐘自己想一遍。
        #: `action=talk` ⇒ 這一跳想開口；**獨白原文不出門**，交給 compose 當動機重新組句
        #: （docs/PLAN-common-ground-20261109.md 第 1 步）。
        self.monologue = monologue
        #: 獨白 talk 的走法：`compose`（預設）＝原文只當動機、由組句器寫給主人看的那句；
        #: `raw`＝舊行為（獨白原文直接過閘）。`BLUEAGENT_MONOLOGUE_TALK` 可覆寫。
        route = str(talk_route if talk_route is not None
                    else os.environ.get("BLUEAGENT_MONOLOGUE_TALK") or "compose").strip().lower()
        self.talk_route = "raw" if route == "raw" else "compose"
        #: `recent_talk(entity) -> str`：最近跟主人聊到哪（給獨白的「最近の会話」；沒接就空）。
        #: 以前那一欄寫死空字串 ⇒ 獨白看不到對話，卻在決定要不要找主人說話。
        self.recent_talk = recent_talk
        #: 這一跳的台詞是哪條路來的（觀測用：`monologue`／`monologue_raw`／`heartbeat`）
        self.last_talk_route = ""
        #: 出口閘（`runtime/inner/premise.py`）：主人只憑他看得到的東西看得懂嗎？看不懂就重寫／不發
        self.premise = premise
        #: `visible(entity) -> {"lines": [...], "fresh": bool}`：主人那邊最近看得到的幾則
        self.visible = visible
        self.last_premise: dict = {}
        #: 模型在獨白裡選了 `action=work` ⇒ 真的去做（owner 2026-11-01：「接誰讓模型自己決定」）
        self.on_self_work = on_self_work
        #: 內驅（`inner/drive.py`）：`drive_base` 是資料根（tasks／works／pending_delivery 都在那），
        #: `on_drive_work(entity, signal)` 回 True＝真的開了一件「核這一條」的事。
        self.drive_base = drive_base
        self.on_drive_work = on_drive_work
        self.drive_gap_s = max(600.0, float(drive_gap_s))
        self.drive_daily_cap = max(0, int(drive_daily_cap))
        #: `drive_running(entity, activity_id)`：內驅開的那件還在做 ⇒ 先不結帳
        self.drive_running = drive_running
        #: 世界變化（`runtime/inner/world_delta.py`）：**觀察者層隨這一跳刷新**（owner 2026-11-03）。
        #: 只算不推錨點——「距上一歩多久」仍然以她真的動手那一刻為準。
        self.world = world
        #: 讓路（owner 2026-11-02）：`busy(entity) -> bool` ＝「主人正在跟她說話」。
        #: 引擎只有一條推論 slot，心跳那幾次呼叫會把主人的回合擠到 deadline 之外（實測
        #: 2026-11-02 22:08：子代理排掉 5.9 s、主腦只剩 38 ms ⇒ 空回）。忙的時候**這一跳
        #: 只做本地推進**（情緒／預算／留痕照走），不碰模型。
        self.busy = busy
        #: 睡眠闸（owner 2026-11-16）：`state(entity, at) -> asleep|winding|waking|awake`。
        #: 睡着时这一跳只做本地推进（AEM／预算／生物钟／留痕），不碰模型——见 `inner/sleep_gate.py`。
        self.sleep_state = sleep_state
        self.last_sleep_state = ""
        #: 主人剛說完的尾巴：這麼多秒內也不要開口（免得回合剛結束就插一句）
        self.quiet_s = max(0.0, float(quiet_s))
        self._yield_until = 0.0
        #: 這一跳到期的事（`compose` 可以拿來當燃料；沒有 compose 就接在模板後面）。
        self.due_now: list[str] = []
        self._due_rows: list[dict] = []
        #: 這一跳的台詞實際帶上的預約（送出後只標這幾筆）
        self._due_used: list[dict] = []
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.ticks = 0
        #: 她上一次在心跳裡開口是第幾跳（0＝不知道）：時間感的「距上次開口」用它
        self.last_spoken_tick = 0
        self._resume_ticks()
        self.spoken = 0
        self.skipped = 0
        self.last: dict = {}
        self.last_skip_reason = ""
        #: 最近一次機率化擲骰的帳（`{"p":…, "roll":…, "ok":…, "rate":…}`）；沒開就空 dict
        self.last_hazard: dict = {}

    # ── 生命週期 ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name=f"ba-heartbeat-{self.entity}")
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._worker is not None and self._worker.is_alive())

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):   # 每跳後依 arousal 重算
            try:
                self.tick()
            except Exception as exc:                      # noqa: BLE001 - 心跳不能炸
                self._log(f"heartbeat[{self.entity}]: 這一跳出錯"
                          f"（{type(exc).__name__}: {exc}）")

    def _apply_arousal_pacing(self) -> None:
        """把「這一跳的有效間隔與門檻」按 arousal 調一下（不是改基準）。

        · 間隔：`base × (1.6 − arousal)`，夾在 0.5×–2.5×（興奮時 15s、低落時 75s/60s 級）
        · 門檻：`0.35 + (1 − arousal) × 0.15` ⇒ 0.30（很興奮）–0.50（很平靜）
        注意：心跳**不是刺激**，這裡只讀 arousal、不回寫。
        """
        if not self.arousal_pacing:
            return
        try:
            snap = self.affect.snapshot(self.entity) or {}
            arousal = float(snap.get("arousal") or 0.0)
        except Exception:                               # noqa: BLE001
            return
        arousal = min(1.0, max(0.0, arousal))
        # 端點對齊 owner 要的數字：arousal=1 → 0.5×（15s）／門檻 0.30；arousal=0 → 2.5×（75s）／0.50
        factor = min(2.5, max(0.5, 2.5 - 2.0 * arousal))
        self.interval_s = max(5.0, self.base_interval_s * factor)
        self.threshold = min(0.50, max(0.30, 0.50 - 0.20 * arousal))
        self.last_effective = {"arousal": round(arousal, 3),
                              "interval_s": round(self.interval_s, 1),
                              "threshold": round(self.threshold, 3)}

    def _apply_energy_brake(self) -> None:
        """預算（精力）低於門檻 ⇒ 這一跳自動降頻、更難開口。

        用意：連續做強度高的自己的事之後，「累」要能反映在行為上——
        不是報錯，而是**她會安靜下來休息**（間隔拉長、門檻拉高）。
        """
        if not self.energy_brake or self.budget is None:
            return
        try:
            balance = float((self.budget.mood(self.entity) or {}).get("balance") or 0.0)
        except Exception:                               # noqa: BLE001
            return
        if balance >= self.rest_below:
            return
        self.interval_s = min(300.0, self.interval_s * 1.8)
        self.threshold = min(0.60, self.threshold + 0.08)
        self.last_effective = {**self.last_effective, "brake": "tired",
                               "balance": round(balance, 2),
                               "interval_s": round(self.interval_s, 1),
                               "threshold": round(self.threshold, 3)}

    # ── 一跳 ────────────────────────────────────────────────────────────────
    def tick(self, *, at: float | None = None, force: bool = False) -> list[dict]:
        """`force=True`＝主人手動叫她說一句（跳過門檻與暖機，仍走去重）。"""
        now = float(at if at is not None else self._clock())
        self.ticks += 1
        if self.beats is not None:
            # 留痕（`runtime/inner/beats.py`）：事後能回指「第 N 跳」，也給提醒用
            self.beats.mark(self.entity, tick=self.ticks, at=now, interval_s=self.interval_s)
        if self.budget is not None:
            # 心跳＝累積：休息也在存她自己的精力額度（安靜時段照存）
            self.budget.accrue(self.entity)
        self.affect.advance(self.entity)                  # ① 衰減／重算
        self._apply_arousal_pacing()                      # ①.5 節奏由 arousal 調（間隔／門檻）
        self._apply_energy_brake()                        # ①.6 精力低 ⇒ 降頻（她累了）
        self._refresh_world(now)                          # ①.55 世界變化：觀察者層隨心跳刷新
        self._note_ignored(now)                           # ①.56 已讀很久沒回 ⇒ 一次 ignored（情感整理 §3-6）
        # ①.65 主人在說話／她在睡 ⇒ 這一跳只做本地推進（不碰模型；睡眠闸見 `inner/sleep_gate.py`）
        if not force and sleep_gate.rest(self, now):
            return []
        scale = sleep_gate.cadence_scale(self, now)       # 睡前醒後是過渡帶：獨白／心情定位降頻 ×2
        due_rows = self._peek_due(now)                    # ①.7 到期的預約（「幾跳前說要提的事」）
        self.due_now = [str(row.get("text") or "") for row in due_rows]
        self._due_rows = due_rows
        self._due_used = []
        mood = self.affect.mood(self.entity, at=now)      # ② 想不想說話
        self.last = mood
        if self.moodline is not None:
            # 心情定位（owner 2026-11-01）：把 v／a 翻成一句人話，**自己判要不要花呼叫**
            #（三條快取條件：Δv/Δa 太小、未滿 240 秒、沒有新意見）。壞了不擋心跳。
            try:
                self.moodline.maybe_read(self.entity, mood, at=now, age_scale=scale,
                                         events=self._mood_events())
            except Exception as exc:                       # noqa: BLE001
                self._log(f"heartbeat[{self.entity}]: moodline 略過（{type(exc).__name__}）")
        # 獨白（`runtime/inner/monologue.py`）：**接哪條路由模型自己決定**（owner 2026-11-01）。
        # `talk` ⇒ 這一跳就用它寫的那段話去過閘（不再另外組台詞）；`work` ⇒ 真的開一件自己的事。
        monologue_talk = ""
        monologue_intent: dict = {}
        monologue_action = ""
        if self.monologue is not None:
            try:
                if self.monologue.due(self.entity, at=now, scale=scale):
                    row = self.monologue.beat(self.entity,
                                              facts=self._monologue_facts(mood), at=now) or {}
                    monologue_action = str(row.get("action") or "")
                    if monologue_action == "talk":
                        monologue_talk = str(row.get("text") or "").strip()
                        monologue_intent = {"text": monologue_talk,
                                            "why": str(row.get("why") or "").strip()}
                    elif monologue_action == "work" and self.on_self_work is not None:
                        try:
                            self.on_self_work(self.entity, row)
                        except Exception as exc:           # noqa: BLE001
                            self._log(f"heartbeat[{self.entity}]: 獨白想做事但沒開成"
                                      f"（{type(exc).__name__}: {exc}）")
            except Exception as exc:                       # noqa: BLE001 - 獨白壞了不擋心跳
                self._log(f"heartbeat[{self.entity}]: 獨白略過（{type(exc).__name__}）")
        if not force and mood["propensity"] < self.threshold:
            # 內驅（owner 2026-11-09）：不是「時間到了就催」，而是有具體未結項才出手；
            # 出手後由 `drive.settle` 結帳，世界沒變就冷卻那個來源（見 inner/drive.py）。
            if self.on_drive_work is not None and drive.attempt(
                    self.entity, self.drive_base, now, on_work=self.on_drive_work,
                    budget=self.budget, log=self._log, gap_s=self.drive_gap_s,
                    daily_cap=self.drive_daily_cap, running=self.drive_running):
                self.last_skip_reason = "drive"
                return []
            self.skipped += 1
            self.last_skip_reason = "below_threshold"
            return []
        # **先過閘再合**：閘很便宜、合很貴（D 之後台詞由她自己組織＝一次模型呼叫）。
        # 順序顛倒的話，被 min_gap／安靜時段擋掉的每一跳都會白叫一次模型（30 秒一次）。
        allowed, reason = (True, "forced") if force else self.gate.allows(
            self.entity, now=now, principal=self.principal)
        if not allowed:
            self.skipped += 1
            self.last_skip_reason = str(reason or "blocked")
            self._log(f"heartbeat[{self.entity}]: 想說但被擋（{self.last_skip_reason}）")
            return []
        # **機率化（owner 2026-11-11）**：閘說「可以」≠「現在就說」——擲輸只是這一跳不說，不叫模型、不耗額度。
        if not force and getattr(self.gate, "hazard", False) and hasattr(self.gate, "roll"):
            ok, info = self.gate.roll(self.entity, now=now, mood=mood, dt=self.interval_s)
            self.last_hazard = dict(info or {})
            if not ok:
                self.skipped += 1
                self.last_skip_reason = "hazard"
                return []
        # 模型自己說「想找他說話」⇒ 那段獨白是**動機**，不是台詞（common-ground 第 1 步）：
        # 獨白是寫給自己的內心話（看得到世界層、私人活動、到期預約），主人一句都沒看過；
        # 原文直接送出就是「跳跃省略／記憶假定」的標準配方。交給組句器按「自然な入り方」重寫。
        if monologue_talk and self.talk_route == "raw":
            text = monologue_talk
            self.last_talk_route = "monologue_raw"
        elif monologue_talk:
            text = self._compose(mood, intent=monologue_intent)
            self.last_talk_route = "monologue"
        else:
            text = self._compose(mood)
            self.last_talk_route = "heartbeat"
        self.last_premise = {}
        if text and self.last_talk_route != "monologue_raw":   # raw＝舊行為，不過出口閘
            text = self._check_premise(text, mood, monologue_intent)
        if not text:
            self.skipped += 1
            self.last_skip_reason = "premise" if self.last_premise.get("verdict") == "dropped" \
                else "no_text"
            return []
        # 在等上一條的回音時不算「寂寞」（common-ground §5.3）：他還沒回 ≠ 他不理我
        waiting = bool(getattr(self.gate, "unanswered", lambda *_a, **_k: [])(self.entity, principal=self.principal))
        urgency = "lonely" if float(mood.get("social_need") or 0.0) >= 0.6 and not waiting else ""
        outcome = self.gate.maybe_send(self.entity, text, principal=self.principal,
                                       publish=self.publish, now=now,
                                       urgency=urgency, force=force)   # ③ 閘
        if not outcome.get("sent"):
            self.skipped += 1
            self.last_skip_reason = str(outcome.get("reason") or "blocked")
            self._log(f"heartbeat[{self.entity}]: 想說但被擋（{self.last_skip_reason}）")
            return []
        self.spoken += 1
        self.last_spoken_tick = self.ticks
        self.last_skip_reason = ""
        if self.beats is not None:
            self.beats.mark(self.entity, tick=self.ticks, at=now, interval_s=self.interval_s,
                            spoke=True)
        # 講出口了才算用掉，而且只算**台詞真的帶上的**那幾筆（沒開口／沒帶上就留到下一跳）
        self._mark_fired(self._due_used, now)
        if self.activity_log is not None:
            self.activity_log.append(self.entity, kind="heartbeat", text=text,
                                      visibility="public", activity="")
        return [outcome.get("frame") or {}]

    def _refresh_world(self, at: float) -> None:
        """世界變化（owner 2026-11-03）：**觀察者層隨這一跳刷新**。

        只算、不推「距上一歩」的錨點——那個錨點只在她真的動手（`WorldDelta.block()`）時推進，
        否則隔幾小時回來，差距會被心跳刷成「30 秒」，時間感就沒了。
        壞了只記在 log，不擋心跳（心跳每一跳都要活著）。
        """
        world = self.world
        if isinstance(world, dict):
            world = world.get(self.entity)
        if world is None:
            return
        try:
            world.observe(now=at)
        except Exception as exc:                          # noqa: BLE001
            self._log(f"heartbeat[{self.entity}]: 世界變化沒算成（{type(exc).__name__}）")

    def _monologue_facts(self, mood: dict) -> dict:
        """給獨白的事實（**不含 v／a**；心情只以人話出現，不變式①）。"""
        words = ""
        if self.moodline is not None:
            try:
                words = str(self.moodline.cached(self.entity).get("read") or "")
            except Exception:                              # noqa: BLE001
                words = ""
        activities: list[str] = []
        if self.activity_log is not None:
            try:
                activities = [str(row)[:60] for row in
                              self.activity_log.private_tail(str(self.entity), limit=3)]
            except Exception:                              # noqa: BLE001
                activities = []
        idle = 0.0
        try:
            idle = float(self.affect.idle_s(self.entity) or 0.0)
        except Exception:                                  # noqa: BLE001
            idle = 0.0
        return {
            "clock": time.strftime("%H:%M", time.localtime()),
            "idle": f"{int(idle // 60)} 分" if idle else "たったいま",
            "mood": words or str(mood.get("label") or ""),
            "activities": activities,
            "talk": self._recent_talk(),
            "open_items": list(self.due_now or []),
            # 世界變化（owner 2026-11-03）：心跳剛刷新過的那一份（不含共享層的時鐘那行，
            # 上面已經有 `clock`）——獨白才知道「這段時間外面動了什麼」。
            "world": self._world_facts(),
            # 提案⑥：台帳的 locked（`waiting` 的依據）＋提案⑤ 的自傳式記憶
            "locked": self_facts.locked_items(self.drive_base, self.entity),
            "self": self_facts.self_block(self.drive_base, self.entity),
        }

    def _check_premise(self, text: str, mood: dict, intent: dict) -> str:
        """出口閘：看不懂 ⇒ 帶著「缺什麼」重寫一次；還看不懂 ⇒ 回空（這一跳不發）。"""
        if self.premise is None:
            return text
        view: dict = {}
        if self.visible is not None:
            try:
                view = dict(self.visible(self.entity) or {})
            except Exception as exc:                       # noqa: BLE001 - 取不到就當沒有可見的對話
                self._log(f"heartbeat[{self.entity}]: 可見對話取不到（{type(exc).__name__}）")
        try:
            earlier = getattr(self.gate, "unanswered_texts", lambda *_a, **_k: [])(
                self.entity, principal=self.principal)
            result = self.premise.review(
                text, visible=view.get("lines") or (), fresh=bool(view.get("fresh")),
                previous=earlier,
                redo=lambda hint: self._compose(mood, intent={**(intent or {}), "fix": hint}))
        except Exception as exc:                           # noqa: BLE001 - 閘壞了 ⇒ 照舊送
            self._log(f"heartbeat[{self.entity}]: 出口閘略過（{type(exc).__name__}）")
            return text
        self.last_premise = {k: result.get(k) for k in ("verdict", "missing")}
        return str(result.get("text") or "")

    def _note_ignored(self, now: float) -> None:
        """她的主動消息主人**讀了、3 小時沒回** ⇒ 每條一次 `ignored`（−0.07；她會少說一點，不會多說）。"""
        if not (hasattr(self.gate, "stale_reads") and hasattr(self.affect, "outcome")):
            return
        try:
            for row in self.gate.stale_reads(self.entity, principal=self.principal, now=now):
                self.affect.outcome(self.entity, kind="concern", delta_v=-0.07, delta_a=0.0,
                                    event_id=f"ignored:{row.get('message_id') or row.get('at')}",
                                    target_type="owner", target_id="waiting")
        except Exception as exc:                           # noqa: BLE001 - 心情壞了不擋心跳
            self._log(f"heartbeat[{self.entity}]: ignored 略過（{type(exc).__name__}）")

    def _recent_talk(self) -> str:
        """最近跟主人聊到哪（給獨白）；沒接或壞了回空字串（獨白照跑）。"""
        if self.recent_talk is None:
            return ""
        try:
            return str(self.recent_talk(self.entity) or "").strip()
        except Exception as exc:                           # noqa: BLE001 - 取不到不擋心跳
            self._log(f"heartbeat[{self.entity}]: 最近の会話取不到（{type(exc).__name__}）")
            return ""

    def _world_facts(self) -> str:
        """心跳剛刷新的世界變化（給獨白）；沒有就空字串。"""
        world = self.world
        if isinstance(world, dict):
            world = world.get(self.entity)
        if world is None:
            return ""
        try:
            return str(world.facts() or "")
        except Exception:                                  # noqa: BLE001
            return ""

    def _mood_events(self, limit: int = 4) -> list[str]:
        """給心情定位的「為什麼」：最近幾筆真正推動過數值的 episode（只取還有效的）。

        只用 `kind`／對象／方向——**不給增量數字**（數字會讓那一句變成翻譯腔，
        而且這一層的目的是人話，不是報表）。拿不到就回空，不擋。
        """
        timeline = getattr(self.affect, "timeline", None)
        if timeline is None:
            return []
        try:
            rows = timeline(self.entity, limit=limit * 3)
        except Exception:                                  # noqa: BLE001
            return []
        now = float(self._clock())
        out: list[str] = []
        for row in rows:
            if float(row.get("expires_at") or 0.0) and float(row["expires_at"]) < now:
                continue
            direction = "上向き" if float(row.get("delta_valence") or 0.0) >= 0 else "下向き"
            target = str(row.get("target") or "")
            out.append(f"{row.get('kind') or 'できごと'}（{direction}{'・' + target if target else ''}）")
            if len(out) >= limit:
                break
        return out

    def _compose(self, mood: dict, intent: dict | None = None) -> str:
        """組這一跳的台詞，並把**帶上了哪幾筆預約**記在 `self._due_used`。

        約定：`compose(entity, mood, due=…)` 收到 `due` ＝那幾筆已經交給她，要不要提由她決定，
        送出後全數算用掉；舊的 `compose(entity, mood)` 沒收到 ⇒ 一筆都不算。
        模板路徑只把**塞進台詞的**那幾筆算用掉（review 2026-10-28：以前只說第一件、卻全標 fired）。

        `intent`：獨白選了 talk 時的那段內心話（`{text, why}`）——**只當動機交給 compose**，
        不收 `intent` 的舊 compose 就當沒有（照舊組句），獨白原文一律不出門。
        """
        rows = list(self._due_rows)
        if self.compose is not None:
            try:
                handed = True
                text = ""
                try:
                    if intent:
                        try:
                            text = str(self.compose(self.entity, mood, due=self.due_now,
                                                    intent=intent) or "").strip()
                        except TypeError:          # 不收 intent 的 compose
                            text = str(self.compose(self.entity, mood,
                                                    due=self.due_now) or "").strip()
                    else:
                        text = str(self.compose(self.entity, mood, due=self.due_now) or "").strip()
                except TypeError:                      # 舊的 `compose(entity, mood)`
                    handed = False
                    text = str(self.compose(self.entity, mood) or "").strip()
                if text:
                    self._due_used = rows if handed else []
                    return text
            except Exception as exc:                     # noqa: BLE001
                self._log(f"heartbeat[{self.entity}]: compose 失敗"
                          f"（{type(exc).__name__}: {exc}）")
        # 低落的模板說不出原因 ⇒ `template()` 退到 ふつう（common-ground §5.1）
        head, body = _template(str(mood.get("label") or "ふつう"))
        text = f"{head}　{body}"
        used = rows[:_TEMPLATE_DUE_MAX]
        if used:
            text = f"{text}（そういえば、{'／'.join(str(row.get('text') or '') for row in used)}）"
        self._due_used = used
        return text

    def _resume_ticks(self) -> None:
        """重啟接續跳號（`BeatLog.resume`）：否則「N 跳後」的預約要從 0 重數。"""
        if self.beats is None or not hasattr(self.beats, "resume"):
            return
        try:
            self.ticks, self.last_spoken_tick = (int(value) for value in self.beats.resume(self.entity))
        except Exception as exc:                           # noqa: BLE001 - 留痕壞了不該擋開機
            self._log(f"heartbeat[{self.entity}]: 跳號接續失敗（{type(exc).__name__}）")

    def _peek_due(self, now: float) -> list[dict]:
        """這一跳到期的預約（**只看不標**：這一跳不一定開口）。"""
        if self.reminders is None:
            return []
        try:
            rows = self.reminders.due(self.entity, tick=self.ticks, now=now)
        except Exception as exc:                           # noqa: BLE001 - 預約壞了不該擋心跳
            self._log(f"heartbeat[{self.entity}]: 預約讀取失敗（{type(exc).__name__}）")
            return []
        return [row for row in rows if str(row.get("text") or "").strip()]

    def _mark_fired(self, rows: list[dict], now: float) -> None:
        if not rows or self.reminders is None:
            return
        try:
            self.reminders.mark_fired(self.entity, rows, now=now)
        except Exception as exc:                           # noqa: BLE001
            self._log(f"heartbeat[{self.entity}]: 預約標記失敗（{type(exc).__name__}）")
            return
        self._log(f"heartbeat[{self.entity}]: 到期預約 {len(rows)} 筆已提")

    def snapshot(self) -> dict:
        return {"entity": self.entity, "ticks": self.ticks, "spoken": self.spoken,
                "last_spoken_tick": self.last_spoken_tick,
                "skipped": self.skipped, "interval_s": self.interval_s,
                "threshold": self.threshold, "running": self.running,
                "last_skip": self.last_skip_reason, "mood": self.last,
                "talk_route": self.talk_route, "last_talk_route": self.last_talk_route,
                "last_premise": dict(self.last_premise)}


from agent.brain.runtime.inner.heartbeat_set import HeartbeatSet  # noqa: E402,F401


#: 每個角色自己的節拍（秒）——實作在 `inner/intervals.py`（本檔已到 600 行上限）
from agent.brain.runtime.inner.intervals import DEFAULT_INTERVALS, interval_for  # noqa: E402,F401


def build_heartbeats(entities, *, affect, gate, publish=None, compose=None,
                     activity_log=None, threshold: float = 0.35, log=None,
                     budget=None, reminders=None, beats=None, moodline=None,
                     monologue=None, on_self_work=None,
                     drive_base=None, on_drive_work=None, drive_gap_s: float = 2700.0,
                     drive_daily_cap: int = 4, drive_running=None,
                     world=None,
                     busy=None, quiet_s: float = 8.0, recent_talk=None, sleep_state=None,
                     premise=None, visible=None) -> HeartbeatSet:
    """每個角色一條。**建構參數裡沒有 entities 迴圈**——一條只管一個實體。"""
    return HeartbeatSet([
        Heartbeat(entity=str(name), affect=affect, gate=gate, publish=publish,
                  compose=compose, activity_log=activity_log,
                  interval_s=interval_for(str(name)), threshold=threshold, log=log,
                  budget=budget, reminders=reminders, beats=beats, moodline=moodline,
                  monologue=monologue, on_self_work=on_self_work,
                  drive_base=drive_base, on_drive_work=on_drive_work,
                  drive_gap_s=drive_gap_s, drive_daily_cap=drive_daily_cap,
                  drive_running=drive_running,
                  world=(world.get(str(name)) if isinstance(world, dict) else world),
                  busy=busy, quiet_s=quiet_s, recent_talk=recent_talk, sleep_state=sleep_state,
                  premise=premise, visible=visible)
        for name in entities
    ])


__all__ = ["DEFAULT_INTERVALS", "Heartbeat", "HeartbeatSet", "build_heartbeats",
           "interval_for"]
