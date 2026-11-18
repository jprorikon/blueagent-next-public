"""開機接線的兩件小事（owner 2026-11-03 抓到的兩條）。

① **她主動說的話要寫回自己的線**：`ProactiveGate` 從前只把句子交給 `publish`，
   沒寫進任何 thread 的 transcript ⇒ 主人回「見る」時她回「何を見る？」（她不知道自己說了什麼）。
② **開機對帳**：`processing` 但對應活動已經不在的任務退回 `hanging`
   ——從前活動被換掉而 `_finish` 沒走到，看板上就留著「同一個標題三筆都在進行中」。

放在獨立檔的原因是 `bootstrap.py` 有 600 行的棘輪（`tests/test_code_size.py`）。
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from agent.brain.compose.proactive_echo import make_proactive_recorder
from agent.brain.runtime.inner.affect import AffectService
from agent.brain.runtime.budget import ActionBudget
from agent.brain.runtime.inner.circadian import build_clocks_from_env
from agent.brain.runtime.proactive import (PROACTIVE_DAILY_MAX, PROACTIVE_DAILY_TARGET,
                                           ProactiveGate)


@dataclass
class LocalCore:
    """她的節奏與精力：生物鐘／主動閘／預算（含存摺鏡射）／情緒。"""

    circadian: dict
    proactive: ProactiveGate
    budget: object
    affect: AffectService


def build_local_core(base, entities, *, log=print) -> LocalCore:
    """建「節奏與精力」那四件（`bootstrap.py` 600 行棘輪 ⇒ 搬出來）。

    生物鐘是 owner 2026-11-08 要的：每個角色一份，落 `data/circadian/<id>.json`，
    睡著的角色不排新活動、不主動開口、群聊不「拿起手機」。
    """
    base = pathlib.Path(base)
    circadian = build_clocks_from_env(base, entities)
    # 機率化搭話（owner 2026-11-11「目标日均条数20条」＋「按時間提升概率」）：
    # 預設開；`BLUEAGENT_PROACTIVE_HAZARD=0` 回到「閘放行就必發」的舊行為（可 A/B）。
    hazard_on = str(os.environ.get("BLUEAGENT_PROACTIVE_HAZARD") or "1").strip().lower() \
        not in {"0", "off", "false", "no"}
    proactive = ProactiveGate(users_dir=base / "users", state_path=base / "proactive.json",
                              warmup_s=float(os.environ.get("BLUEAGENT_PROACTIVE_WARMUP_S") or 0.0),
                              min_gap_s=float(os.environ.get("BLUEAGENT_PROACTIVE_MIN_GAP_S")
                                              or 20 * 60),
                              daily_max=int(os.environ.get("BLUEAGENT_PROACTIVE_DAILY_MAX")
                                            or PROACTIVE_DAILY_MAX),
                              hazard=hazard_on,
                              daily_target=float(os.environ.get("BLUEAGENT_PROACTIVE_DAILY_TARGET")
                                                 or PROACTIVE_DAILY_TARGET),
                              circadian=circadian)
    budget = ActionBudget(base / "budget", log=lambda message: log(f"  {message}"))
    # 存摺（owner 2026-11-01 第③項）：只記帳、不擋人；關掉就設 BLUEAGENT_RP=0。
    if str(os.environ.get("BLUEAGENT_RP") or "1").strip().lower() not in {"0", "off", "false", "no"}:
        from agent.brain.runtime.inner.rp_ledger import LedgerMirror, RpLedger
        ledger = RpLedger(base / "rp",
                          rate_mrp=int(os.environ.get("BLUEAGENT_RP_RATE_MRP") or 100),
                          tick_s=int(os.environ.get("BLUEAGENT_RP_TICK_S") or 300),
                          log=lambda message: log(f"  {message}"))
        budget = LedgerMirror(budget, ledger)
    affect = AffectService(base / "affect",
                           enabled=str(os.environ.get("BLUEAGENT_AFFECT") or "1").strip()
                           not in {"0", "off", "false"})
    proactive.affect = affect
    return LocalCore(circadian=circadian, proactive=proactive, budget=budget, affect=affect)


def wire_proactive_echo(proactive, threads, *, log=print) -> None:
    """她主動發出的句子，寫回她自己的那條線（寫不上就只是不記，不影響發送）。"""
    if proactive is None:
        return
    try:
        proactive.record = make_proactive_recorder(
            threads, log=lambda message: log(f"  {message}"))
    except Exception as exc:                       # noqa: BLE001 - 接線壞了不擋開機
        log(f"  proactive: 回寫接線失敗（{type(exc).__name__}）")


def reconcile_tasks(ledgers, activity_store, entities, *, log=print) -> int:
    """把「還掛著 processing、但活動已經不在」的任務退回 hanging。回回收筆數。"""
    active_ids: list[str] = []
    #: 正在跑的活動身上的 task_id（舊資料裡正在做的那筆沒有 activity_id，要靠這個認）
    task_ids: list[str] = []
    try:
        for entity in entities.all():
            row = activity_store.load(entity.id)
            activity_id = str(getattr(row, "id", "") or "") if row is not None else ""
            if activity_id:
                active_ids.append(activity_id)
            meta = (getattr(row, "meta", None) or {}) if row is not None else {}
            if isinstance(meta, dict) and meta.get("task_id"):
                task_ids.append(str(meta["task_id"]))
    except Exception as exc:                       # noqa: BLE001
        log(f"  tasks: 讀活動失敗（{type(exc).__name__}）")
        return 0
    total = 0
    for name, ledger in (ledgers or {}).items():
        try:
            fixed = ledger.reconcile(active_ids, task_ids)
        except Exception as exc:                   # noqa: BLE001
            log(f"  tasks[{name}]: 對帳失敗（{type(exc).__name__}）")
            continue
        if fixed:
            log(f"  tasks[{name}]: 回收 {fixed} 筆沒接上的 processing")
        total += fixed
    return total


def after_threads(*, proactive, threads, ledgers, activity_store, entities, outbox=None,
                  log=print) -> None:
    """線開好之後要接的三件事（主動消息回寫 + 任務對帳 + 未收尾分句）。"""
    wire_proactive_echo(proactive, threads, log=log)
    reconcile_tasks(ledgers, activity_store, entities, log=log)
    if outbox is not None:
        recover_outbox(outbox, log=log)


def recover_outbox(outbox, *, log=print) -> int:
    """開機對帳：上一輪在收尾前被重啟 ⇒ 分句檔還在、正式列沒有。

    owner 2026-11-04：「後台發出分句，後台就落檔這一句」——那些句子已經落檔了，
    只是還沒收成正式那一列；這裡把它們補成一列（`ok=False`），不要隨重啟消失。
    對帳失敗不該擋開機。
    """
    run = getattr(outbox, "recover_pending", None)
    if not callable(run):
        return 0
    try:
        fixed = int(run() or 0)
    except Exception as exc:                          # noqa: BLE001
        log(f"outbox: 開機對帳失敗（{type(exc).__name__}: {exc}）")
        return 0
    if fixed:
        log(f"outbox: 補回 {fixed} 輪未收尾的分句紀錄")
    return fixed


__all__ = ["after_threads", "wire_proactive_echo", "reconcile_tasks", "recover_outbox"]
