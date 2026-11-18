"""串行生活迴圈：**一次只推進一步；人一來就停在邊界；斷點落盤；之後接著做。**

這不是對話調度器（那個在 `scheduler.py`），而是「她自己的事」的執行器：

    ActivityStore  ──load/save──┐
                                ▼
    LifeLoop（每實體一條執行緒）: 取當前步 → RUNNING → step_fn → DONE → cursor+1 → 寫盤
                                ▲
    human_enter() ──pause───────┘   （不強殺進行中的一步；它做完才停）
    human_exit()  ──resume──────┘
    pause()/resume()                 （手動）
    重啟：load 到 RUNNING → 降級 PAUSED，當前步退回 PENDING（重試）

「串行」是硬約束：同一實體同時只有一條 LifeLoop、一個 step 在跑。
"""

from __future__ import annotations

import inspect
import threading
import time
from typing import Callable

from agent.brain.domain.activity import Activity, ActivityStatus, StepStatus
from agent.brain.runtime.activity_store import ActivityStore


def _takes_previous(fn) -> bool:
    """`next_activity` 接不接兩個參數（`entity, previous`）？只看簽名，不看回傳。"""
    if fn is None:
        return False
    try:
        parameters = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = [item for item in parameters
                  if item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2


class LifeLoop:
    def __init__(self, entity: str, store: ActivityStore,
                 step_fn: Callable[[Activity, object], str], *,
                 step_pause: float = 0.35, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] | None = None,
                 next_activity: Callable[..., "Activity | None"] | None = None,
                 between_pause: float = 60.0,
                 budget=None, step_cost: float = 1.0,
                 idle_pause: float = 1800.0, wake_in=None,
                 resume_beats: int = 3, ledger=None, circadian=None) -> None:
        self.entity = str(entity)
        self.store = store
        self._step_fn = step_fn
        self._step_pause = max(0.0, float(step_pause))
        self._clock = clock
        self._log = log or (lambda _message: None)
        #: 做完一件之後**接著做下一件**（owner 2026-10-22：她不能做完一件就停住）。
        #: `None`＝維持舊行為（做完就待著，等外面叫她）。
        self._next_activity: Callable[..., Activity | None] | None = next_activity
        #: `next_activity` 想收前一件活動（輪換要知道上一套是什麼）⇒ 兩參數版本優先。
        self._next_takes_previous = _takes_previous(next_activity)
        self._between_pause = max(0.0, float(between_pause))
        #: `next_activity` 回 None（今天做滿了）時要休息多久再問一次
        self._idle_pause = max(60.0, float(idle_pause))
        #: 休息中的「醒來提示」：回「還有多久會有事」（秒）。預定時刻到了就不必睡滿 30 分鐘
        #  （owner 2026-11-03：「預定表工具应该可以返回来 kick 她们自己」）。
        self._wake_in = wake_in if callable(wake_in) else None
        #: 最早可以開工的時刻（做完一件之後的留白）
        self._resume_at = 0.0
        #: 行動預算（自主行動要消耗；沒有就等她存夠——不是報錯）
        self._budget = budget
        self._step_cost = float(step_cost)
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._activity: Activity | None = store.load(self.entity)
        self._paused = False
        self._paused_by_human = False
        self._human = 0
        #: 被打斷之後要**隔幾個心跳**才回斷點（owner 2026-11-03：「數個心跳後回到斷點繼續」）。
        #: 不能人一走就立刻續——那等於她一邊回話一邊做自己的事。
        self._resume_beats = max(0, int(resume_beats))
        self._beat_seconds = max(30.0, float(step_pause))
        #: 任務台帳（owner 2026-11-03）：開工／被打斷／完成都要在台帳上留狀態，
        #: 不然「她手上有什麼」只有日誌裡有，她自己看不到。
        self._ledger = ledger
        #: 她自己的生物钟（owner 2026-11-08）：睡着的时候不排新活动、不推进下一步；
        #: 醒来照常。聊天不受这条限制（主人在端上说话她照样答）。
        self._circadian = circadian
        self._asleep_logged = False
        self._error = ""
        self._recovered = bool(self._activity.meta.get("recovered")) \
            if self._activity is not None else False
        if self._activity is not None and self._activity.status is ActivityStatus.RUNNING:
            # 上次沒有正常收尾（進程被殺、斷線）：當前步重試，不假裝做完了。
            self._activity.status = ActivityStatus.PAUSED
            self._activity.meta["recovered"] = True
            self._activity.checkpoint(self._clock())
            self.store.save(self._activity)
            self._recovered = True
            # 留白之後自己續（不是永遠等人來叫）：先等 `between_pause` 秒
            self._resume_at = self._clock() + self._between_pause
            self._log(f"life: 檢測到未完成的活動「{self._activity.title}」"
                      f"（第 {self._activity.cursor + 1} 步），等她回來繼續")
        # store 在載入時就把「上次 RUNNING」降級成 PAUSED（meta.recovered=True）⇒
        # 復原的等待時間要在這裡補，否則她會永遠停在 paused（2026-10-22 實測）
        if (self._recovered and self._activity is not None
                and self._activity.status is ActivityStatus.PAUSED):
            self._resume_at = self._clock() + self._between_pause
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name=f"life-{self.entity}")
        self._worker.start()
        # 開機就有一件沒做完的 ⇒ 在台帳上標成 processing（不然它一直掛在 hanging 上）
        if self._ledger is not None and self._activity is not None \
                and self._activity.status is not ActivityStatus.DONE:
            try:
                self._ledger.on_start(self._activity)
                self.store.save(self._activity)
            except Exception:                      # noqa: BLE001 - 台帳壞了不擋做事
                pass

    # ── 對外 ────────────────────────────────────────────────────────────────────
    def start(self, activity: Activity) -> None:
        with self._cond:
            self._activity = activity
            self._resume_at = 0.0                    # 手上空著時排的「休息到幾點」不帶進新的一件
            self._paused = False
            self._paused_by_human = False
            self._error = ""
            self._recovered = False
            activity.status = ActivityStatus.RUNNING
            activity.checkpoint(self._clock())
            self.store.save(activity)
            if self._ledger is not None:
                try:
                    self._ledger.on_start(activity)
                    self.store.save(activity)
                except Exception:                  # noqa: BLE001
                    pass
            self._cond.notify_all()
        self._log(f"life: 開始「{activity.title}」· {activity.total} 步")

    def pause(self, reason: str = "manual") -> None:
        with self._cond:
            self._paused = True
            self._paused_by_human = False
            if self._activity is not None:
                self._activity.pause(self._clock())
                self._activity.meta["pause_reason"] = reason
                self.store.save(self._activity)
            self._cond.notify_all()
        self._log(f"life: 暫停（{reason}）")

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._paused_by_human = False
            if self._activity is not None:
                self._activity.resume(self._clock())
                self.store.save(self._activity)
            self._cond.notify_all()
        self._log("life: 從檢查點繼續")

    def human_enter(self) -> None:
        """人開口了：停在安全邊界（當前 step 做完），存檢查點。"""
        with self._cond:
            self._human += 1
            if self._activity is not None and self._activity.status is ActivityStatus.RUNNING:
                self._paused_by_human = True
                self._activity.pause(self._clock())
                self._activity.meta["pause_reason"] = "human"
                self._save_checkpoint(reason="human")
                if self._ledger is not None:
                    try:
                        self._ledger.on_interrupt(self._activity)
                    except Exception:              # noqa: BLE001
                        pass
                self.store.save(self._activity)
            self._cond.notify_all()

    def human_exit(self) -> None:
        """人說完了：**不立刻續**，先沉幾個心跳，再回到斷點。

        以前是「人一走下一跳就接著做」，結果是她的活動跟對話黏在一起——她還在回話的餘韻裡
        就跑去寫筆記（owner 2026-11-03 要的是「數個心跳後回到斷點繼續」）。
        """
        with self._cond:
            self._human = max(0, self._human - 1)
            if self._human == 0 and self._paused_by_human and not self._paused:
                wait = self._resume_beats * self._beat_seconds
                self._resume_at = self._clock() + wait
                if self._activity is not None:
                    resume = self._activity.meta.get("resume")
                    if isinstance(resume, dict):
                        resume["resume_at"] = self._resume_at
                    self.store.save(self._activity)
                self._log(f"life: 對話結束，{self._resume_beats} 拍後"
                          f"（約 {int(wait)}s）從斷點續做")
            self._cond.notify_all()

    def reset(self) -> None:
        with self._cond:
            self._activity = None
            self._paused = False
            self._paused_by_human = False
            self._error = ""
            self._recovered = False
            self.store.clear(self.entity)
            self._cond.notify_all()

    def can_open_new(self, now: float | None = None) -> bool:
        """現在能開新的一件嗎（生物鐘：睡著、睡前一小時／醒後半小時都不開）。內驅用。"""
        clock_of = getattr(self, "_circadian", None)
        if clock_of is None:
            return True
        moment = self._clock() if now is None else float(now)
        try:
            return not (clock_of.asleep(moment) or clock_of.settling_in(moment))
        except Exception:                            # noqa: BLE001 - 生物鐘壞了不擋
            return True

    def wake_now(self) -> bool:
        """手上空著（或剛做完）⇒ 不等休息到點，馬上照常排下一件（台帳先：續 hanging、撿 pending）。

        走的是平常那條 `next_activity`，所以「心情不好先不做」與生物鐘照樣會擋。回有沒有叫醒。
        """
        with self._cond:
            activity = self._activity
            idle = activity is None or activity.done or activity.status is ActivityStatus.FAILED
            if not idle or self._human or self._paused_by_human:
                return False
            self._resume_at = self._clock()
            self._cond.notify_all()
        return True

    def current(self) -> Activity | None:
        """現在這一件（給呼叫端看 `id`／`meta`，例如輪換要知道上一套是哪一套）。"""
        with self._cond:
            return self._activity

    def snapshot(self) -> dict:
        with self._cond:
            activity = self._activity.to_dict() if self._activity is not None else None
            return {
                "entity": self.entity,
                "activity": activity,
                "paused": self._paused or self._paused_by_human,
                "paused_by_human": self._paused_by_human,
                "human_active": self._human > 0,
                "recovered": self._recovered,
                "error": self._error,
                "checkpoint_at": activity["checkpoint_at"] if activity else 0,
            }

    def shutdown(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._worker.join(timeout=2.0)

    def _next(self, previous):
        """排下一件。兩參數版本會拿到**剛做完的那一件**（輪換要排除它）。"""
        if self._next_activity is None:
            return None
        if self._next_takes_previous:
            return self._next_activity(self.entity, previous)
        return self._next_activity(self.entity)

    def _cost_of(self, activity) -> float:
        """這一類行動的價錢（價格表在 `ActionBudget.cost_for`）。"""
        if self._budget is None:
            return float(self._step_cost)
        kind = str(getattr(activity, "kind", "") or "")
        cost_for = getattr(self._budget, "cost_for", None)
        if cost_for is None:
            return float(self._step_cost)
        return float(cost_for(kind, default=self._step_cost))

    # ── 內部 ────────────────────────────────────────────────────────────────────
    def _save_checkpoint(self, *, reason: str) -> None:
        """被打斷時把「做到哪」寫成一個斷點：第幾步、下一步是什麼、上一步的結果。

        為什麼要另存一份 `meta["resume"]`：`cursor` 只說「第幾步」，
        但續做的時候她需要知道「上一句我自己寫了什麼」，否則會從頭再想一遍。
        """
        activity = self._activity
        if activity is None:
            return
        steps = list(getattr(activity, "steps", ()) or ())
        cursor = int(getattr(activity, "cursor", 0) or 0)
        done = [item for item in steps[:cursor]
                if str(getattr(item, "note", "") or "").strip()]
        nxt = steps[cursor].title if cursor < len(steps) else ""
        activity.meta["resume"] = {
            "cursor": cursor,
            "next_step": nxt,
            "last": (str(getattr(done[-1], "note", "") or "")[:200] if done else ""),
            "reason": str(reason),
            "saved_at": self._clock(),
            "after_beats": self._resume_beats,
        }

    def _idle_wait(self) -> float:
        """休息多久：預設 `idle_pause`，但**有預定時刻快到就縮短**（最多睡到那一刻）。

        owner 2026-11-03：預定表要能把她自己 kick 起來，不能因為「今天做滿了」睡滿 30 分鐘。
        """
        base = max(60.0, float(self._idle_pause))
        if self._wake_in is None:
            return base
        try:
            soon = float(self._wake_in())
        except Exception:                          # noqa: BLE001 - 提示壞了不該擋住休息
            return base
        return max(60.0, min(base, soon))

    def _runnable(self) -> bool:
        # 生物鐘（owner 2026-11-08）：睡着就不排新活動、不推進（醒來自己續）。
        clock_of = getattr(self, "_circadian", None)
        if clock_of is not None and clock_of.asleep(self._clock()):
            if not getattr(self, "_asleep_logged", False):
                window = clock_of.snapshot(self._clock())
                self._log(f"life[{self.entity}]: 睡了（作息 {window['sleep']}–{window['wake']}）"
                          " ⇒ 今晚不排新活動")
                self._asleep_logged = True
            return False
        if getattr(self, "_asleep_logged", False):
            self._asleep_logged = False
            self._log(f"life[{self.entity}]: 醒了 ⇒ 回到自己的節奏")
        if clock_of is not None:
            try:
                clock_of.settle(self._clock())       # 按時睡的日子作息慢慢回落（一天一次，A6）
            except Exception:                        # noqa: BLE001 - 生物鐘壞了不擋活動
                pass
        # 被人打斷：人在說話 ⇒ 不做；人走了也要**數個心跳之後**才回斷點
        if self._paused_by_human:
            if self._human:
                return False
            if self._clock() >= float(self._resume_at or 0.0):
                self._paused_by_human = False
                activity = self._activity
                if activity is not None:
                    activity.status = ActivityStatus.RUNNING
                    activity.checkpoint(self._clock())
                    self.store.save(activity)
                    resume = activity.meta.get("resume") or {}
                    self._log(f"life: 回到斷點（第 {int(resume.get('cursor', 0)) + 1} 步"
                              f"・{str(resume.get('next_step') or '')[:24]}）")
                return True
            return False
        if self._human:
            return False
        activity = self._activity
        if activity is None:
            # 手上沒有事（開機時排不出來／選了先不做）⇒ 休息到點再問一次，不要永遠停住
            if self._next_activity is None or (clock_of is not None and clock_of.settling_in(self._clock())):
                return False
            if self._resume_at <= 0:                 # 第一次發現手上空著 ⇒ 先休息一段再問（不馬上搶）
                self._resume_at = self._clock() + self._idle_wait()
                return False
            return self._clock() >= self._resume_at
        now = self._clock()
        # ④ 預算不夠 ⇒ 先不要動（等她心跳存夠；主人在端上派的事不走這條）
        cost = self._cost_of(activity)
        if self._budget is not None and not self._budget.allows(self.entity, cost):
            return False
        # ③ 上次沒收尾（PAUSED + recovered）⇒ 留白之後自己續，不要永遠卡住
        if (activity.status is ActivityStatus.PAUSED and self._recovered
                and not self._paused_by_human):
            if now >= max(self._resume_at, 0.0):
                activity.status = ActivityStatus.RUNNING
                self._paused = False
                return True
            return False
        # ① 正常推進
        if activity.status is ActivityStatus.RUNNING and not self._paused:
            return True
        # ② 做完了（或這一步失敗了）而且有「下一件」⇒ 要能轉場，不然她做完一件就停住
        if self._next_activity is not None and (activity.done
                                                or activity.status is ActivityStatus.FAILED):
            # 睡前一小時／醒後半小時只收尾、不開新的一件（A8：以前 winding 沒有任何人看）
            if clock_of is not None and clock_of.settling_in(now):
                return False
            return now >= self._resume_at
        return False

    def _note_bedtime(self, activity) -> None:
        """一件活動收尾 ⇒ 告訴生物鐘「她做到幾點」，熬過入睡點就讓明天整體後移。"""
        clock_of = getattr(self, "_circadian", None)
        if clock_of is None or activity is None:
            return
        try:
            shift = clock_of.note_activity_end(self._clock())
        except Exception as exc:                      # noqa: BLE001
            self._log(f"life[{self.entity}]: 生物鐘沒記成（{type(exc).__name__}）")
            return
        if shift:
            self._log(f"life[{self.entity}]: 生物鐘後移 {shift} 分（昨天做得晚）")

    def _charge_activity_once(self, activity) -> None:
        """一件活動只收一次錢（`src` 帶活動 id ⇒ 存摺天然冪等，重啟／重入都不會重複記）。"""
        if self._budget is None or activity is None:
            return
        meta = activity.meta if isinstance(getattr(activity, "meta", None), dict) else {}
        if meta.get("charged"):
            return
        meta["charged"] = True
        try:
            self._budget.try_spend(self.entity, self._cost_of(activity),
                                   reason=str(getattr(activity, "title", "") or ""),
                                   src=f"activity:{activity.id}")
        except Exception as exc:                          # noqa: BLE001 - 記帳失敗不擋活動
            self._log(f"life[{self.entity}]: 這次沒記成（{type(exc).__name__}）")

    def _finish(self, activity: Activity) -> None:
        """一件活動走到頭了：**完成**還是**收工**（失敗三分法的 `goal_invalid`）。

        以前這裡只有一種結局（DONE ＋ 台帳 completed）——於是她「試了三次都不成就該放下」的
        那件事，永遠在台帳上留著、永遠被撿回來重做。現在 `meta["abort"]` 一擺上就改成：
        活動 FAILED、台帳記一次失敗（`attempts` 到頂就變 outdated，不再撿）。
        """
        meta = getattr(activity, "meta", None)
        abort = meta.get("abort") if isinstance(meta, dict) else None
        if abort:
            activity.status = ActivityStatus.FAILED
            meta["error"] = (f"abort:{abort.get('kind')}"
                             f"（{int(abort.get('failures') or 0)}回）")
            if self._ledger is not None:              # 台帳：這一次沒做成
                try:
                    self._ledger.on_failed(activity)
                except Exception:                     # noqa: BLE001
                    pass
            self._log(f"life: 「{activity.title}」收工（{abort.get('kind')}·"
                      f"{int(abort.get('failures') or 0)} 回）")
        else:
            activity.status = ActivityStatus.DONE
            if self._ledger is not None:              # 台帳：這件 completed
                try:
                    self._ledger.on_done(activity)
                except Exception:                     # noqa: BLE001
                    pass
        self._settle_network(activity)                # 外網明細併成一筆收（owner 2026-11-01）
        self._note_bedtime(activity)                  # 生物鐘：她做到几点（熬过入睡点⇒明天后移）
        activity.checkpoint(self._clock())
        self.store.save(activity)

    def _settle_network(self, activity) -> None:
        """外網明細併成一筆收（owner 2026-11-01 照 aster 的設計改）。

        aster：「一步 1 RP ＋ 同一步三次外網 4.5 ⇒ 帳面看不出『這步很貴』，只看到四筆扣款」
        ⇒ 一步只留一筆帶明細。明細本身已經累在 `activity.meta['net_steps']`。
        """
        if self._budget is None or activity is None:
            return
        meta = activity.meta if isinstance(getattr(activity, "meta", None), dict) else {}
        points = float(meta.get("net_points") or 0.0)
        if points <= 0 or meta.get("net_settled"):
            return
        meta["net_settled"] = True
        hits = int(meta.get("net_hits") or 0)
        try:
            charge = getattr(self._budget, "charge", None)
            if charge is not None:
                charge(self.entity, points, reason=f"外網 ×{hits}（本回合明細）",
                       src=f"activity:{activity.id}:net")
            else:
                self._budget.try_spend(self.entity, points,
                                       reason=f"外網 ×{hits}（本回合明細）",
                                       src=f"activity:{activity.id}:net")
        except Exception as exc:                          # noqa: BLE001
            self._log(f"life[{self.entity}]: 外網明細沒記成（{type(exc).__name__}）")

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                while not self._stop.is_set() and not self._runnable():
                    self._cond.wait(0.25)
                if self._stop.is_set():
                    return
                activity = self._activity
                step = activity.current if activity is not None else None
                if activity is None or step is None:
                    if activity is not None:
                        self._finish(activity)
                    if self._next_activity is not None:
                        # 收尾 → 換下一件（中間留一段空白，別像機器人排滿）
                        try:
                            nxt = self._next(self._activity)
                        except Exception as exc:      # noqa: BLE001
                            self._log(f"life: 下一件排不出來（{type(exc).__name__}: {exc}）")
                            nxt = None
                        if nxt is not None:
                            self._activity = nxt
                            self._activity.start(self._clock())
                            self.store.save(self._activity)
                            self._paused = False
                            self._paused_by_human = False
                            # 中間留白：記一個「最早開工時間」，由 `_runnable` 守（不睡、不用 Timer）
                            self._resume_at = self._clock() + max(0.0, self._between_pause)
                            self._paused = False
                            self._paused_by_human = False
                        else:
                            # 今天做夠了（輪換回 None）：留一段空白再問一次。
                            # 沒有這一行會每 0.25 秒重問一次（空轉），也會把「今天到這裡」演成當機。
                            wait = self._idle_wait()
                            self._resume_at = self._clock() + wait
                            self._log("life: 下一件先不排（做滿了／都還太近／沒有新材料／気が乗らない）"
                                      "⇒ 休息 %.0f 分鐘" % (wait / 60.0))
                    continue
                step.status = StepStatus.RUNNING
                step.started_at = self._clock()
                activity.checkpoint(self._clock())
                self.store.save(activity)
                # 記帳單位＝**一次活動（回合）**（owner 2026-11-01 照 aster 的設計改）。
                # 以前每一步都收一次 ⇒ 一件 5 步的事被收 5 倍價，等於「按步計價」；
                # aster 的單位是「一次呼叫」⇒ 建議我們往上挪一層。現在：**一件只收一次**，
                # 步內免費；外網明細在結束時併成一筆（`_settle_network`）。
                self._charge_activity_once(activity)

            note = ""
            try:
                note = str(self._step_fn(activity, step) or "")
            except Exception as exc:                   # noqa: BLE001 - 一步失敗不該炸整條 life
                with self._cond:
                    step.status = StepStatus.FAILED
                    step.ended_at = self._clock()
                    activity.status = ActivityStatus.FAILED
                    activity.meta["error"] = f"{type(exc).__name__}: {exc}"
                    if self._ledger is not None:          # 台帳：這一步沒成 ⇒ 退回 hanging（下次再試）
                        try:
                            self._ledger.on_failed(activity)
                        except Exception:                 # noqa: BLE001
                            pass
                    self._settle_network(activity)
                    activity.checkpoint(self._clock())
                    self.store.save(activity)
                    self._error = activity.meta["error"]
                    self._paused = True
                    self._cond.notify_all()
                continue

            with self._cond:
                activity.advance(self._clock(), note=note)
                if isinstance(getattr(activity, "meta", None), dict) and activity.meta.get("abort"):
                    # 這一步判定「不該再做」（失敗三分法的 `goal_invalid`）⇒ 不再往下走：
                    # cursor 推到尾，下一圈走「收工」那段（標 FAILED、台帳記一次失敗）。
                    activity.cursor = activity.total
                self.store.save(activity)
                self._cond.notify_all()
            if self._step_pause:
                time.sleep(self._step_pause)
