"""行動預算：**她自己的精力額度**（owner 2026-10-22）。

三檔規則（owner 原話）：

  ┌───────────────────────────┬──────────┬──────────┬──────────┬────────────┐
  │ 行動                       │ 發起源    │ 所屬      │ 消費      │ 預算        │
  ├───────────────────────────┼──────────┼──────────┼──────────┼────────────┤
  │ 心跳                       │ 自己      │ 自己      │ 自己      │ **累積**    │
  │ 自主行動（自己去讀書…）      │ 自己      │ 自己      │ 自己      │ **消耗**    │
  │ 他人發起（主人在端上派她     │ 他人      │ 自己      │ 自己      │ **不受限**  │
  │ 進 TRPG、語音來的長鏈任務）  │          │          │          │            │
  └───────────────────────────┴──────────┴──────────┴──────────┴────────────┘

為什麼要這樣分：她「想做什麼」是有限資源（不然會一天讀完 30 本書），
但「被人拜託的事」不是她自己的興致——用預算去擋那件事，等於她會拒絕主人。

實作要點：
  · 每 **一個角色一份**（`data/budget/<entity>.json`，0600、原子寫）。
  · **累積看經過的時間，不看跳了幾次**：`accrue()` 用 `now - last_at` 算增益
    （速率 1 點 / 30 秒，可用 `BLUEAGENT_BUDGET_SECONDS_PER_POINT` 覆寫）。
    心跳／任何 tick 只是**呼叫**它；因此把心跳間隔按 arousal 調成 15–75s 時，
    **經濟完全不變**（原本是「跳一次 +1 點」⇒ 間隔一改，預算就翻倍或腰斬）。
  · 消耗走 `try_spend()`；不夠就回 False，讓呼叫端**等她存夠**（不是報錯）。
  · 他人發起的行動**根本不經過這裡**（呼叫端判來源），所以不會被擋。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import tempfile
import threading
import time

#: 默認參數（都可用環境變數覆寫）。
PER_TICK = 1.0           # 舊語意：每次心跳累積多少（保留給「明確指定 amount」的呼叫端）
SPP_DEFAULT = 30.0       # 每 30 秒累積 1 點（與心跳間隔無關）
CAP = 24.0               # 上限（存太多就沒有「取捨」了）
START = 6.0              # 新角色起始額度
STEP_COST = 1.0          # 自主行動一「步」的價錢（沒有專門價格時的預設）

#: 價格表（owner 2026-10-22 待辦「預算價格表」）：不同種類的自主行動**價錢不一樣**。
#: 理由：額度是「取捨」用的。若每件事都 1 點，她會照順序做完，沒有「今天要不要出門」的選擇；
#: 拉開價差之後，貴的事要存幾跳才做得起，她自己就會權衡。
#: kind 來自 `runtime/life.py` 的 Preset.kind（study / play / ambient）＋ 之後的 excursion。
PRICES: dict[str, float] = {
    "ambient": 0.5,      # 日常（整理、閒聊、看看窗外）
    "study": 1.0,        # 查資料／學習
    "play": 1.0,         # 自己玩
    "social": 1.0,       # 角色之間的私聊（owner 2026-11-08：算雙方的自主活動）
    "excursion": 2.0,    # 出門（之後才會有）
}
SPEAK_COST = 0.0         # 心跳自己的搭話：不算錢（它就是心跳的一部分）


def _slug(value: str, fallback: str = "unknown") -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return clean or fallback


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class ActionBudget:
    """每個角色一份額度；累積靠心跳，消耗靠自主行動。"""

    def __init__(self, root, *, per_tick: float | None = None, cap: float | None = None,
                 start: float | None = None, log=None, clock=time.time) -> None:
        self.root = pathlib.Path(root)
        self.per_tick = _env_float("BLUEAGENT_BUDGET_PER_TICK", PER_TICK) \
            if per_tick is None else float(per_tick)
        self.cap = _env_float("BLUEAGENT_BUDGET_CAP", CAP) if cap is None else float(cap)
        self.start = _env_float("BLUEAGENT_BUDGET_START", START) if start is None else float(start)
        #: 沒有專門價格時的一步價錢（價格表見 `PRICES`／`cost_for`）
        self.step_cost = _env_float("BLUEAGENT_BUDGET_STEP_COST", STEP_COST)
        self._log = log or (lambda _message: None)
        self._clock = clock
        self._lock = threading.RLock()

    # ── 檔案 ────────────────────────────────────────────────────────────────
    def path_for(self, entity: str) -> pathlib.Path:
        return self.root / f"{_slug(entity)}.json"

    def _load(self, entity: str) -> dict:
        path = self.path_for(entity)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {"balance": float(raw.get("balance") or 0.0),
                        "accrued": float(raw.get("accrued") or 0.0),
                        "spent": float(raw.get("spent") or 0.0),
                        "ticks": int(raw.get("ticks") or 0),
                        # 時間基準：沒有就當「現在」，避免第一次 accrue 一次補一大包
                        "last_at": float(raw.get("last_at") or self._clock())}
        except (OSError, ValueError):
            pass
        # 新檔案：時間基準就是「建立的那一刻」。不給的話 `accrue()` 的
        # `state.get("last_at") or now` 會把第一次結算的區間整段丟掉。
        return {"balance": self.start, "accrued": 0.0, "spent": 0.0,
                "ticks": 0, "last_at": self._clock()}

    def _save(self, entity: str, state: dict) -> None:
        path = self.path_for(entity)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"entity": str(entity), "updated_at": round(self._clock(), 3),
                **{key: round(float(value), 3) if isinstance(value, float) else value
                   for key, value in state.items()}}
        data = json.dumps(body, ensure_ascii=False, indent=1) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".budget-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(data)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    # ── 累積（心跳）──────────────────────────────────────────────────────────
    def accrue(self, entity: str, amount: float | None = None, *,
               now: float | None = None) -> dict:
        """累積預算。**預設看時間**（`amount=None`）；給了 `amount` 才是舊的「加固定值」。"""
        now = self._clock() if now is None else float(now)
        with self._lock:
            state = self._load(entity)
            if amount is None:
                spp = _env_float("BLUEAGENT_BUDGET_SECONDS_PER_POINT", SPP_DEFAULT)
                last = float(state.get("last_at") or now)
                elapsed = max(0.0, now - last)
                delta = elapsed / max(1.0, spp)
                state["last_at"] = now
            else:
                delta = float(amount)
            state["balance"] = min(self.cap, state["balance"] + max(0.0, delta))
            state["accrued"] += max(0.0, delta)
            state["ticks"] += 1
            state["last_gain"] = round(max(0.0, delta), 3)
            self._save(entity, state)
            return self.mood(entity, state=state)

    # ── 消耗（自主行動）──────────────────────────────────────────────────────
    def cost_for(self, kind: str = "", *, default: float | None = None) -> float:
        """這種行動要多少錢：`BLUEAGENT_BUDGET_PRICES="study=1.5,play=1"` 可覆寫。"""
        table = dict(PRICES)
        raw = str(os.environ.get("BLUEAGENT_BUDGET_PRICES") or "").strip()
        if raw:
            for chunk in raw.split(","):
                if "=" not in chunk:
                    continue
                name, _, value = chunk.partition("=")
                try:
                    table[name.strip().lower()] = float(value)
                except ValueError:
                    continue
        base = float(self.step_cost if default is None else default)
        return float(table.get(str(kind or "").strip().lower(), base))

    def allows(self, entity: str, cost: float = STEP_COST) -> bool:
        with self._lock:
            return self._load(entity)["balance"] >= float(cost)

    def try_spend(self, entity: str, cost: float = STEP_COST, *, reason: str = "",
                  src: str = "") -> bool:
        """扣一次。`src` 只給**記帳層**用的冪等鍵（存摺靠它不重複記），這裡不參與判斷。"""
        amount = float(cost)
        with self._lock:
            state = self._load(entity)
            if state["balance"] < amount:
                self._log(f"budget[{entity}]: 不夠（餘 {state['balance']:.1f} < {amount:.1f}）"
                          f"⇒ {reason or '等她存夠'}")
                return False
            state["balance"] -= amount
            state["spent"] += amount
            self._save(entity, state)
            return True

    def charge(self, entity: str, cost: float = STEP_COST, *, reason: str = "",
               src: str = "") -> float:
        """**一定要落到帳上**的扣款，可以扣成負的（欠著）。

        為什麼要有這條（owner 2026-10-25「調高使用網路的體力消耗」）：網路費是**事後**才知道
        多少（一步裡叫了幾次工具），`try_spend` 在餘額不夠時會整個拒絕 ⇒ 那一筆就消失了，
        變成「沒錢也能一直查」。這裡改成先欠著：餘額為負就再也跑不動自主活動
        （`_runnable` 要求 `balance >= cost`），等心跳把債還完才繼續。回傳實際扣掉的金額。
        """
        amount = max(0.0, float(cost))
        if amount <= 0:
            return 0.0
        with self._lock:
            state = self._load(entity)
            state["balance"] -= amount
            state["spent"] += amount
            self._save(entity, state)
            if state["balance"] < 0:
                self._log(f"budget[{entity}]: 欠 {abs(state['balance']):.1f} 點"
                          f"（{reason or '先記帳'}）⇒ 還完之前不做自主活動")
            return amount

    # ── 讀數 ────────────────────────────────────────────────────────────────
    def mood(self, entity: str, *, state: dict | None = None) -> dict:
        data = state if state is not None else self._load(entity)
        return {"entity": str(entity), "balance": round(float(data["balance"]), 2),
                "cap": self.cap, "per_tick": self.per_tick,
                "accrued": round(float(data["accrued"]), 2),
                "spent": round(float(data["spent"]), 2),
                "ticks": int(data["ticks"])}

    def snapshot(self, entities=()) -> dict:
        names = list(entities) or [path.stem for path in
                                   (self.root.glob("*.json") if self.root.exists() else [])]
        return {"entities": {name: self.mood(name) for name in names},
                "cap": self.cap, "step_cost": _env_float("BLUEAGENT_BUDGET_STEP_COST", STEP_COST)}


__all__ = ["ActionBudget", "CAP", "PER_TICK", "SPEAK_COST", "START", "STEP_COST"]
