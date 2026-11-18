"""席位本體：kick 入口、入座／離席、每一手的流程、狀態與健康。"""

from __future__ import annotations

import threading
import time

from agent.adapter.trpg.config import TrpgConfig
from agent.adapter.trpg.decide import choose_action, render_view
from agent.adapter.trpg.mood import GAME_AFFECT_SCALE, SeatMood
from agent.adapter.trpg.prompt import SeatPrompts
from agent.adapter.trpg.report import SeatReporting


class TrpgSeat(SeatMood, SeatReporting, SeatPrompts):
    """一席的橋：kick → 看牌面 → 她說話 → 挑合法動作 → （被拒就改一手）。"""

    def __init__(self, config: TrpgConfig, table, speak, *, log=None,
                 max_retries: int = 1, report=None, report_every_rounds: int = 1,
                 report_min_gap_s: float = 90.0, intensity=None,
                 min_intensity: float = 0.12, affect=None, mood=None, notice=None,
                 on_leave=None, on_phase=None,
                 refuse_below: float = -0.08, refuse_cooldown_s: float = 1800.0) -> None:
        self.config = config
        self.table = table
        self.speak = speak                  # `speak(prompt, purpose=...) -> str`
        self._log = log or (lambda _message: None)
        self.max_retries = max(0, int(max_retries))
        #: 匯報出口：`report(text, meta) -> bool`（主人在群裡就貼群、否則 DM）。None＝不匯報。
        self.report = report
        #: 每幾輪匯報一次（1＝每輪一次）；`report_min_gap_s` 是最小間隔（防洗版）。
        self.report_every_rounds = max(1, int(report_every_rounds))
        #: 情緒強度取樣（0–1）。owner 2026-10-22：「設一個較高的情緒門檻，超過才報」
        #: ⇒ 只有真的被牽動（緊張／興奮／難過）才主動講，平盤就不吵。
        self.intensity = intensity
        #: 把遊戲事件記進 AEM：`affect(kind, delta_v, delta_a, payload)`。None＝不記。
        self.affect = affect
        #: 心情取樣：`mood(entity) -> {"valence":…, "arousal":…}`。None＝不判。
        self.mood = mood
        #: 對主人明說「不想玩」的出口：`notice(text) -> None`（走主動訊息那條：App → DM）。
        self.notice = notice
        #: 她離席時的通知（讓 App 把桌畫面收掉、回聊天）：`on_leave(reason) -> None`
        self.on_leave = on_leave
        #: 桌階段變化：`on_phase(kind, round_no)`；只由組合根投給這桌的參與者。
        self.on_phase = on_phase
        self._last_phase = None
        #: 心情低於這個 valence 就拒絕開局（owner 2026-10-22：「情緒低可以拒絕玩，且要明說」）。
        #: 2026-11-09 按 AEM 稽核後的尺重標：「しょんぼり」線是 −0.04，這裡取它的兩倍＝真的低落。
        #: 舊值 −0.25 是滿量程的尺，實測從沒到過 ⇒ 她從來不會拒絕。
        self.refuse_below = float(refuse_below)
        #: 拒絕後的冷卻（秒），免得每手都被踢一次、每次都要再拒絕一次。
        self.refuse_cooldown_s = float(refuse_cooldown_s)
        self._declined_until = 0.0
        self.declines = 0
        #: 遊戲情緒乘區（1.0＝不打折）；預設 0.35 ⇒ 桌上一樣的事只算三分之一多一點
        self.game_affect_scale = GAME_AFFECT_SCALE
        #: 這一局裡每種事件發生過幾次（負面事件用它做磨損）
        self._event_counts: dict[str, int] = {}
        #: 匯報門檻：max(|valence|, arousal)。2026-11-09 重標成「高喚醒」那條線（0.12，`affect._label`）；
        #: 舊值 0.6 實測從沒到過（本機 14 天最高 0.47）⇒ 一次都不會報。
        self.min_intensity = max(0.0, float(min_intensity))
        self.last_intensity = 0.0
        self.report_min_gap_s = max(0.0, float(report_min_gap_s))
        self._last_report_at = 0.0
        self._last_report_round = -1
        self.kicks = 0
        self.acted = 0
        self.said = 0
        self.rejected = 0
        #: 被引擎拒絕過的動作 → 次數（照 `agent_harness/agents_play.py`：拒兩次以上這一局別再用）
        self.bans: dict[str, int] = {}
        self.skipped = 0
        self.errors = 0
        self.reports = 0
        self.last: dict = {}
        #: 一手在跑的時候不要再疊一手（kick 可能重送；桌面也會補踢）。
        self._lock = threading.Lock()
        self._new_game()

    def _new_game(self) -> None:
        """換桌／換 session（新的一局）：這一局才有意義的帳全部歸零。

        2026-11-09 檢查：以前換桌不清 ⇒ 新桌的事件 seq 從 1 開始、全被當成「看過了」；
        死亡磨損跨局一直疊（打折被翻成放大）；上一局被拒的動作在新局也被禁。
        """
        self._seen_seq = None            # 第一眼只立基線（`SeatMood._absorb_events`）
        self._event_counts = {}
        self.bans = {}
        self._last_report_round = -1
        self._last_phase = None

    # ── 對外 ────────────────────────────────────────────────────────────────
    def handle_kick(self, payload: dict | None = None) -> dict:
        """收到一次叫醒（也可以沒有 payload：桌面自己輪到她就走這條）。"""
        self.kicks += 1
        if not self._lock.acquire(blocking=False):
            # 一手還在跑：**不採用**這次 kick 的桌／席位（以前先 adopt 才搶鎖 ⇒ 跑到一半的那一手
            # 後面的 say／act 會被送到新桌、新席位）。桌端會補踢。
            self.skipped += 1
            self.last = {"ok": True, "skipped": "busy"}
            return self.last
        try:
            # kick 的 payload 帶著 table_id／session ⇒ **當場採用**（在鎖裡）。
            # 這樣容器不必為了「這一次坐哪一桌」重啟（桌與 session 每局都不一樣）。
            self.adopt(payload or {})
            return self.step(payload or {})
        except Exception as exc:                            # noqa: BLE001 - 一手炸掉不能斷桌
            self.errors += 1
            self.last = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._log(f"trpg: 這一手失敗（{self.last['error']}）")
            return self.last
        finally:
            self._lock.release()

    def adopt(self, payload: dict) -> None:
        """從 kick payload 更新這一局的桌與 session（換桌就換）。"""
        table = str(payload.get("table_id") or "").strip()
        session = str(payload.get("session") or "").strip()
        changed = False
        if table and table != self.config.table_id:
            self.config.table_id = table
            self.table.config.table_id = table
            changed = True
            self._log(f"trpg: 換桌 {table}")
        if session and session != self.config.session:
            self.config.session = session
            self.table.config.session = session
            changed = True
        if changed:
            self._new_game()
        # 席位也要跟着 kick 走：容器啟動時只知道預設席位（p1），真正的席位是 kick 帶來的
        # （手機／Discord 讓她在哪一席入座，就該用那一席出手——2026-10-22 實測：不吃 seat
        # 會變成「p1 去 act p5 的桌」，引擎直接拒。）
        seat = str(payload.get("seat") or "").strip()
        if seat and seat != self.config.seat:
            self.config.seat = seat
            self.table.config.seat = seat
            self._log(f"trpg: 換席位 {seat}")



    def _publish_context(self, view: dict) -> None:
        """把「這一局現在怎麼了」寫進獨立的遊戲段（不吃對話的滾動預算，局終即回收）。"""
        try:
            from agent.brain.runtime import game_context
            lines = [render_view(view, self.config.seat)]
            acts = view.get("allowed_actions") or []
            if acts:
                lines.append("（使える行動：" + "、".join(str(a) for a in acts[:12]) + "）")
            game_context.set_block("trpg", "\n".join(lines))
        except Exception:                               # noqa: BLE001 - 這段壞了不能擋對話
            pass

    def forget_table(self, *, reason: str = "") -> None:
        """桌不在了：清掉 table／session 綁定（她的狀態回到「沒在玩」）。"""
        if not self.config.table_id and not self.config.session:
            return
        self._log(f"trpg: 桌不在了（{reason or 'gone'}）⇒ 清掉綁定 {self.config.table_id}")
        try:                                            # 局終 ⇒ 回收那一段上下文
            from agent.brain.runtime import game_context
            game_context.clear_block("trpg")
        except Exception:                               # noqa: BLE001
            pass
        self.config.table_id = ""
        self.config.session = ""
        self._new_game()
        inner = getattr(self.table, "config", None)      # 測試用的替身沒有 config
        if inner is not None:
            inner.table_id = ""
            inner.session = ""
        self.last = {"ok": True, "skipped": "no_table"}

    def _note_phase(self, view: dict) -> None:
        """第一次只看基線；同輪重複 kick 不重發，換桌／session 會重置基線。"""
        table = view.get("table") or {}
        round_no = int(view.get("round_no") or table.get("roundNo")
                       or table.get("round_no") or 0)
        phase = ("ended", 0) if view.get("ended") else ("round", round_no)
        previous, self._last_phase = self._last_phase, phase
        if previous is None or previous == phase or self.on_phase is None:
            return
        try:
            self.on_phase(str(phase[0]), int(phase[1]))
        except Exception as exc:                            # noqa: BLE001 - 通知不能擋遊戲
            self._log(f"trpg: 階段通知失敗（{type(exc).__name__}: {exc}）")

    def step(self, kick: dict) -> dict:
        try:
            view = self.table.view()
        except Exception as exc:                            # noqa: BLE001
            # 桌被刪／hub 重啟／session 過期 ⇒ **把綁定清掉**，不要留著一個不存在的桌
            # （否則 kick 亂入時她會一直「在某張桌上」，狀態看起來還在玩）。
            self.forget_table(reason=f"{type(exc).__name__}: {str(exc)[:60]}")
            self.last = {"ok": True, "skipped": "no_table", "error": str(exc)[:120]}
            return self.last
        self._note_phase(view)
        if view.get("ended"):
            # 這局結束了 ⇒ 也不要再掛著那張桌（她的狀態回到「沒在玩」）
            self.forget_table(reason="ended")
            self.last = {"ok": True, "skipped": "ended"}
            return self.last
        self._publish_context(view)
        self._absorb_events(view)
        if self._maybe_decline(view):
            return self.last
        turn = view.get("turn") or {}
        if str(turn.get("seat") or "") != self.config.seat:
            self.skipped += 1
            self.last = {"ok": True, "skipped": "not_my_turn", "turn": turn.get("seat")}
            return self.last
        if view.get("you_acted"):
            # 這一輪已經出手過 ⇒ 交棒（不然會卡在同一格）
            self.table.act("end_turn", note="手番を渡す")
            self.last = {"ok": True, "skipped": "you_acted"}
            return self.last

        prompt = self._prompt(view, kick)
        # 量耗時：桌的 deadline 是硬限制，沒有數字就只能猜「是不是模型太慢」
        _t0 = time.time()
        _err = ""
        try:
            text = str(self.speak(prompt, purpose="trpg") or "").strip()
        except Exception as exc:                        # noqa: BLE001 - 模型炸了要知道原因
            text, _err = "", f"{type(exc).__name__}: {str(exc)[:120]}"
        _speak_s = time.time() - _t0
        self.timing = {"speak_s": round(_speak_s, 2), "prompt_chars": len(prompt),
                       "text_chars": len(text), "error": _err,
                       "round": (view.get("table") or {}).get("roundNo")
                                or (view.get("table") or {}).get("round_no")}
        self._log(f"trpg: 一手耗時 speak={_speak_s:.1f}s prompt={len(prompt)}字 "
                  f"→ 台詞 {len(text)} 字" + (f"（錯誤：{_err}）" if _err else ""))
        if not text:
            self.note_empty = getattr(self, "note_empty", 0) + 1
            self.table.act("end_turn", note="（何も言わなかった）")
            self.last = {"ok": True, "skipped": "no_text", "error": _err,
                         "speak_s": round(_speak_s, 2)}
            return self.last
        decision = choose_action(view, text)
        # 台詞先說（說錯話不會被引擎拒；動作才可能被拒）
        try:
            self.table.say(text[:280])
            self.said += 1
        except Exception as exc:                            # noqa: BLE001
            self._log(f"trpg: table_say 失敗（{type(exc).__name__}: {exc}）")

        result = self.table.act(decision["action"], target=decision.get("target"))
        self.acted += 1
        if result.get("error"):
            self.rejected += 1
            # 記的是**我們送出去的那個動作**（以前讀引擎回覆裡的 `action`，拒絕回覆沒有這欄 ⇒ 永遠記不到）
            self._ban(decision.get("action"))
            self._log(f"trpg: 引擎拒絕（{result['error']}）⇒ 讓她改一手")
            view2 = self.table.view()
            fix = str(self.speak(
                self._prompt(view2, kick, rejected=str(result["error"]))
                + "\n\n（前の手はルールに拒否された。本当にできそうな別の行動に変えて、一言で。）",
                purpose="trpg-retry") or "").strip()
            decision2 = choose_action(view2, fix or text)
            if fix:
                try:
                    self.table.say(fix[:280])
                except Exception:                           # noqa: BLE001
                    pass
            result = self.table.act(decision2["action"], target=decision2.get("target"))
            self.acted += 1
            decision = decision2
            if result.get("error"):
                self._ban(decision2.get("action"))
                self._log(f"trpg: 改手後仍被拒（{result['error']}）⇒ 交棒")
                self.table.act("end_turn", note="手を変えても拒否された")
        self.last = {"ok": True, "say": text[:120], "action": decision.get("action"),
                     "target": decision.get("target"),
                     "engine_error": result.get("error") or ""}
        self._pass_if_still_my_turn()
        self._maybe_report(view)
        return self.last




    def _ban(self, action) -> None:
        name = str(action or "").strip()
        if name and name.lower() not in {"end_turn", "pass", "wait"}:
            self.bans[name] = self.bans.get(name, 0) + 1

    def _pass_if_still_my_turn(self) -> None:
        """出手後如果還是她的回合就**交棒**。

        這一桌的 `turn.end_turn_on_action=false`（不自動交棒）而且 `move` 是 free action
        ⇒ 她走一格之後回合還在她身上，桌面不會再踢她（2026-10-22 實測：整桌卡在 p1、第 1 輪）。
        所以出手後再看一次牌面，還輪到她就 `end_turn`。
        """
        try:
            after = self.table.view()
        except Exception as exc:                            # noqa: BLE001
            self._log(f"trpg: 交棒前讀牌面失敗（{type(exc).__name__}: {exc}）")
            return
        if after.get("ended"):
            return
        if str((after.get("turn") or {}).get("seat") or "") != self.config.seat:
            return
        try:
            self.table.act("end_turn", note="手番を渡す")
        except Exception as exc:                            # noqa: BLE001
            self._log(f"trpg: 交棒失敗（{type(exc).__name__}: {exc}）")


    def seat_state(self) -> dict:
        """給 App 問一句：「她現在願不願意上桌？」

        **當場看心情**，不只看冷卻旗標——App 這邊是使用者主動點桌，
        那時候她還沒被 kick 過，光看 `_declined_until` 會永遠回「願意」。
        """
        now = time.time()
        cooldown = now < self._declined_until
        valence = 0.0
        if self.mood is not None:
            try:
                valence = float((self.mood() or {}).get("valence") or 0.0)
            except Exception:                           # noqa: BLE001
                valence = 0.0
        declined = cooldown or (self.mood is not None and valence <= self.refuse_below)
        if declined and not cooldown:
            # 這次是「App 主動問」才發現的：補記一次、冷卻起來、把話講給主人
            self.declines += 1
            self._declined_until = now + self.refuse_cooldown_s
            if not getattr(self, "_last_line", ""):
                self._last_line = "……ごめん、今日はゲームをする気になれない。"
            if self.notice is not None:
                try:
                    self.notice(self._last_line)
                except Exception:                       # noqa: BLE001
                    pass
        return {"declined": bool(declined),
                "last_line": str(getattr(self, "_last_line", "") or ""),
                "valence": round(valence, 3),
                "declines": self.declines}

    def health(self) -> dict:
        return {"enabled": self.config.enabled, "ready": self.config.ready,
                "seat": self.config.seat, "table": self.config.table_id,
                "session": self.config.session, "kicks": self.kicks,
                "acted": self.acted, "said": self.said,
                "rejected": self.rejected, "skipped": self.skipped,
                "errors": self.errors, "reports": self.reports,
                "min_intensity": self.min_intensity,
                "last_intensity": round(self.last_intensity, 3), "last": self.last,
                "timing": getattr(self, "timing", {}),
                "empty_text": getattr(self, "note_empty", 0),
                "declines": self.declines, "declined": time.time() < self._declined_until}


__all__ = ["TrpgSeat"]
