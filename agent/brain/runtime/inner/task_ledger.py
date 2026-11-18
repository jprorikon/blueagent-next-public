"""她的任務台帳（owner 2026-11-03）：**先查自己手上還有什麼，再決定做什麼**。

為什麼要它：以前她「時間到了就開一套 preset」，等於每天從零開始——
被打斷的那件事、等對方回覆的那件事、暫時做不下去的那件事，全都沒有住處。
現在每一件事都在台帳上有一個狀態：

    pending    排著，還沒開始
    processing 正在做（有 `activity_id`）
    hanging    掛著：被打斷／做了一半，**優先續做**
    locked     被外部條件鎖住（等人回話、等某個時間）⇒ `wait_until` 到了才解鎖
    outdated   不再有意義（放太久、被新任務取代）⇒ 不撿
    completed  做完了（**永久留著**：一週後她想回想「我做過什麼」靠它）

狀態只往前走，唯一的回頭是 `hanging → processing`、`locked → pending`（條件到了）。

存放：`data/tasks/<entity>.json`（一檔一角色，原子寫入；壞檔就重建，不擋做事）。
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import secrets
import threading
import time
from contextlib import contextmanager

STATES = ("pending", "processing", "hanging", "locked", "outdated", "completed")
#: 撿任務的優先序：先續做，再撿鎖解開的，最後排新的
PICK_ORDER = {"hanging": 0, "pending": 1}
#: 放多久沒動就算 outdated（owner 2026-11-03：不要讓半年前的想法還排在隊裡）
STALE_DAYS = float(os.environ.get("BLUEAGENT_TASK_STALE_DAYS") or 14.0)
#: 失敗幾次就判 outdated（一直做不成的，不要無限重試）
MAX_ATTEMPTS = int(os.environ.get("BLUEAGENT_TASK_MAX_ATTEMPTS") or 3)


def _now() -> float:
    return time.time()


class TaskLedger:
    """一檔一角色；所有改動都走 `patch()`，寫完立刻落盤（斷電也不會丟狀態）。

    ⚠️ **同一份檔有兩個行程在寫**：應用（工具面＋LifeLoop＋`/api/tasks`，常駐一份實例）與
    `tools/mcp_tasks_server.py`（每次呼叫新建一份）。所以：
      · 讀之前看檔案有沒有被別人改過（mtime／大小），改過就重讀（`_fresh`）；
      · 寫一律在 `_writing()` 裡：拿檔案鎖 → 重讀 → 改 → 存，不會拿舊快取蓋掉對方剛寫的。
    """

    def __init__(self, root, entity: str, *, clock=_now) -> None:
        self.root = pathlib.Path(root)
        self.entity = str(entity)
        self._clock = clock
        self.path = self.root / f"{self.entity}.json"
        self._items: list[dict] = []
        self._stamp: tuple[int, int] | None = None
        self._lock = threading.RLock()
        self._depth = 0
        self._load()

    # ── 讀寫 ──────────────────────────────────────────────────────────────────
    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else raw
            self._items = [dict(item) for item in (items or ()) if isinstance(item, dict)]
        except (OSError, ValueError):
            self._items = []
        self._stamp = self._file_stamp()

    def _fresh(self) -> None:
        """別的行程（MCP server）改過這份檔 ⇒ 重讀；沒改就沿用快取。"""
        with self._lock:
            if self._file_stamp() != self._stamp:
                self._load()

    @contextmanager
    def _writing(self):
        """一次改動的臨界區：行程內 RLock ＋ 跨行程 flock，進來先重讀。可重入（巢狀呼叫只鎖一次）。"""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_suffix(".lock"), "a", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                self._depth = 1
                try:
                    self._load()
                    yield
                finally:
                    self._depth = 0
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"entity": self.entity, "updated_at": round(self._clock(), 3),
                   "items": self._items}
        # 暫存檔帶行程號：兩個行程同時寫時不會寫進同一個 .tmp
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        self._stamp = self._file_stamp()

    def items(self) -> list[dict]:
        self._fresh()
        return [dict(item) for item in self._items]

    def counts(self) -> dict[str, int]:
        self._fresh()
        counts = {state: 0 for state in STATES}
        for item in self._items:
            counts[str(item.get("state") or "pending")] = \
                counts.get(str(item.get("state") or "pending"), 0) + 1
        return counts

    def summary(self) -> str:
        """給 prompt 看的一段（她「有意識地知道」自己手上有什麼）。"""
        self._fresh()
        counts = self.counts()
        lines = [f"### 自分のタスク台帳（{len(self._items)} 件）",
                 "・" + "／".join(f"{state} {counts.get(state, 0)}"
                                 for state in STATES if counts.get(state))]
        hanging = [item for item in self._items if item.get("state") == "hanging"]
        if hanging:
            lines.append("中断して続き待ち：")
            lines += [f"  - 「{str(item.get('title'))[:32]}」（第 {int(item.get('cursor', 0)) + 1} 歩から）"
                      for item in hanging[:3]]
        locked = [item for item in self._items if item.get("state") == "locked"]
        if locked:
            lines.append("条件待ち：")
            lines += [f"  - 「{str(item.get('title'))[:32]}」（{str(item.get('note') or '')[:24]}）"
                      for item in locked[:3]]
        pending_comments = self.unseen_comments()
        if pending_comments:
            lines.append(f"主人からの批注 {len(pending_comments)} 件（まだ見ていない・どうするかは自分で決める）：")
            lines += [f"  - 「{str(row.get('title'))[:24]}」→ {str(row.get('text'))[:80]}"
                      for row in pending_comments[:3]]
        return "\n".join(lines)

    # ── 兩個視圖：看板（按狀態）＋ 日曆（按日子）────────────────────────────────
    def board(self, *, limit_per_column: int = 8) -> dict:
        """看板：一列一個狀態。她「先看自己手上有什麼」就是看這個。"""
        self._fresh()
        columns: dict[str, list[dict]] = {state: [] for state in STATES}
        for item in sorted(self._items, key=lambda row: -float(row.get("updated_at") or 0)):
            state = str(item.get("state") or "pending")
            comments = list(item.get("comments") or ())
            columns.setdefault(state, []).append(
                {"id": item.get("id"), "title": item.get("title"),
                 "cursor": int(item.get("cursor") or 0), "steps": int(item.get("steps") or 0),
                 "note": item.get("note") or "",
                 "due": _day(float(item.get("due_at") or 0)),
                 # 主人的批注（owner 2026-11-03）：App 只批注、不改狀態
                 "comments": [{"text": row.get("text"), "at": float(row.get("at") or 0),
                                "seen": float(row.get("seen_at") or 0) > 0}
                               for row in comments[-3:]],
                 "unseen_comments": sum(1 for row in comments
                                        if float(row.get("seen_at") or 0) <= 0),
                 # 誰加的、為什麼（防自旋 ⑥）與試了幾次——管理頁要看得出「這件是她自己生出來的」
                 "origin": str(item.get("origin") or ""), "because": str(item.get("because") or ""),
                 "attempts": int(item.get("attempts") or 0), "failures": int(item.get("failures") or 0),
                 "updated_at": float(item.get("updated_at") or 0)})
        return {"counts": self.counts(),
                "unseen_comments": len(self.unseen_comments()),
                "columns": {state: rows[:limit_per_column]
                            for state, rows in columns.items()}}

    def calendar(self, *, days: int = 7, now: float | None = None) -> dict:
        """日曆：按 `due_at` 分日；沒定日子的放 `unscheduled`（她可以自己排進去）。"""
        self._fresh()
        moment = float(now if now is not None else self._clock())
        buckets: dict[str, list[dict]] = {}
        unscheduled: list[dict] = []
        horizon = moment + max(1, int(days)) * 86400.0
        for item in self._items:
            if str(item.get("state")) in ("completed", "outdated"):
                continue
            due = float(item.get("due_at") or 0)
            if not due:
                unscheduled.append(_brief(item))
                continue
            if due > horizon:
                continue                                  # 遠期規劃另外用 `days` 放大才看得到
            buckets.setdefault(_day(due), []).append(_brief(item))
        return {"today": _day(moment), "days": days,
                "by_day": {day: rows for day, rows in sorted(buckets.items())},
                "unscheduled": unscheduled[:12]}

    # ── 寫 ────────────────────────────────────────────────────────────────────
    def self_added_today(self, *, now: float | None = None) -> int:
        """今天她自己（`origin="self"`）加了幾件（防自旋 ⑥：一天最多 2 件）。"""
        import time as _time
        moment = float(now if now is not None else self._clock())
        day = _time.localtime(moment)[:3]
        self._fresh()
        return sum(1 for item in self._items if str(item.get("origin") or "") == "self"
                   and _time.localtime(float(item.get("created_at") or 0.0))[:3] == day)

    def add(self, title: str, *, preset: str = "", priority: float = 0.0,
            state: str = "pending", note: str = "", due_at: float = 0.0,
            origin: str = "", because: str = "") -> dict:
        """登記一件事。**同一個 preset 還有沒做完的就回收那一筆**（owner 2026-11-03）。

        為什麼：活動輪換會在固定幾個 preset 之間來回，而從前每次回到同一個 preset 就再登記
        一筆 ⇒ 看板上出現「ストアページを見て…」×5、「集めた数字を表にする」×4 這種同標題堆疊。
        這裡只對「還沒結束」（pending／processing／hanging）的去重；已經 completed／outdated
        的是歷史紀錄，照樣各記一筆（她真的又做了一次，該留下來）。
        """
        now = self._clock()
        key = str(preset or "").strip()
        if key and state in {"pending", "processing", "hanging"}:
            self._fresh()
            for item in self._items:
                if str(item.get("preset") or "") != key:
                    continue
                if str(item.get("state") or "") not in {"pending", "processing", "hanging"}:
                    continue
                # 已經有同一個 preset 的未完成任務 ⇒ 就是它（沿用，不新增）
                return dict(item)
        item = {"id": f"task_{secrets.token_hex(4)}", "title": str(title),
                "preset": str(preset), "state": str(state) if state in STATES else "pending",
                "priority": float(priority), "created_at": now, "updated_at": now,
                "cursor": 0, "activity_id": "", "attempts": 0, "failures": 0, "note": str(note),
                #: `due_at`＝**打算什麼時候做**（日曆那一軸）；`wait_until`＝**被什麼卡住**（locked 用）
                "due_at": float(due_at or 0.0), "wait_until": 0.0,
                #: 誰加的、為什麼（防自旋 ⑥：她自己加的要寫是哪個新材料促成的）
                "origin": str(origin or ""), "because": str(because or "")[:200]}
        item["comments"] = []
        with self._writing():
            self._items.append(item)
            self._save()
        return dict(item)

    def get(self, task_id: str) -> dict | None:
        self._fresh()
        for item in self._items:
            if str(item.get("id")) == str(task_id):
                return dict(item)
        return None

    def patch(self, task_id: str, **fields) -> dict | None:
        with self._writing():
            for item in self._items:
                if str(item.get("id")) != str(task_id):
                    continue
                if "state" in fields and str(fields["state"]) not in STATES:
                    fields.pop("state")
                item.update(fields)
                # 明著給 `updated_at` 就照給（回填／測試要造「放很久」的狀態）
                if "updated_at" not in fields:
                    item["updated_at"] = self._clock()
                self._save()
                return dict(item)
        return None

    def set_state(self, task_id: str, state: str, *, note: str = "",
                  wait_until: float = 0.0) -> dict | None:
        fields: dict = {"state": state}
        if note:
            fields["note"] = str(note)[:200]
        if wait_until:
            fields["wait_until"] = float(wait_until)
        with self._writing():                # 讀 attempts 與寫回要在同一把鎖裡
            if state == "processing":
                fields["attempts"] = int((self.get(task_id) or {}).get("attempts", 0)) + 1
            return self.patch(task_id, **fields)

    # ── 主人的批注（owner 2026-11-03：App 只能「批注」，怎麼處理她自己判斷）──────
    def comment(self, task_id: str, text: str, *, by: str = "owner") -> dict | None:
        """主人在某一條任務上留一句話。**不直接改狀態**——她自己看了再決定。"""
        with self._writing():
            item = self.get(task_id)
            if item is None:
                return None
            rows = list(item.get("comments") or [])
            rows.append({"by": str(by or "owner"), "text": str(text)[:300],
                         "at": self._clock(), "seen_at": 0.0})
            return self.patch(task_id, comments=rows)

    def unseen_comments(self) -> list[dict]:
        """還沒被她看到的批注（她「下次檢查」就是把這個吃掉）。"""
        self._fresh()
        out = []
        for item in self._items:
            for row in item.get("comments") or ():
                if float(row.get("seen_at") or 0) > 0:
                    continue
                out.append({"id": item.get("id"), "title": item.get("title"),
                            "state": item.get("state"), "text": row.get("text"),
                            "by": row.get("by"), "at": float(row.get("at") or 0)})
        return out

    def ack_comments(self, task_id: str = "") -> int:
        """把批注標成「看過了」（她處理完之後呼叫）。回標了幾條。"""
        moment = self._clock()
        marked = 0
        with self._writing():
            for item in self._items:
                if task_id and str(item.get("id")) != str(task_id):
                    continue
                rows = list(item.get("comments") or [])
                for row in rows:
                    if float(row.get("seen_at") or 0) <= 0:
                        row["seen_at"] = moment
                        marked += 1
                if rows:
                    item["comments"] = rows
            if marked:
                self._save()
        return marked

    # ── 選下一個 ──────────────────────────────────────────────────────────────
    def unlock_due(self, now: float | None = None) -> list[str]:
        """`wait_until` 到了的 locked ⇒ 回 pending。回被解鎖的 id。"""
        moment = float(now if now is not None else self._clock())
        freed = []
        with self._writing():
            for item in self._items:
                if item.get("state") != "locked":
                    continue
                due = float(item.get("wait_until") or 0)
                if due and due <= moment:
                    item["state"] = "pending"
                    item["updated_at"] = moment
                    freed.append(str(item.get("id")))
            if freed:
                self._save()
        return freed

    def mark_stale(self, now: float | None = None) -> list[str]:
        """放太久沒動的 pending ⇒ outdated；失敗太多次的也一樣。"""
        moment = float(now if now is not None else self._clock())
        limit = STALE_DAYS * 86400.0
        stale = []
        with self._writing():
            for item in self._items:
                state = str(item.get("state") or "")
                if state not in ("pending", "hanging", "locked"):
                    continue
                if int(item.get("attempts") or 0) >= MAX_ATTEMPTS:
                    item["state"] = "outdated"
                    item["note"] = f"やり直し {int(item.get('attempts'))} 回で打ち切り"
                    stale.append(str(item.get("id")))
                    continue
                if moment - float(item.get("updated_at") or moment) > limit:
                    item["state"] = "outdated"
                    item["note"] = "しばらく手をつけていないので古い話になった"
                    stale.append(str(item.get("id")))
            if stale:
                self._save()
        return stale

    def due_tasks(self, now: float | None = None, *, within_s: float = 0.0) -> list[dict]:
        """予定の時刻が来た（または近い）タスク——**彼女自身を kick する**ための一覧。

        `due_at` は今までカレンダー表示にしか使っていなかった（owner 2026-11-03：
        「予定表工具应该可以返回来 kick 她们自己」）。ここで拾って、活動として起動する。
        `within_s` は「あと N 秒以内に始まる」も前倒しで拾うため。
        """
        self._fresh()                                  # MCP 那一側剛排進來的也要看得到
        moment = float(now if now is not None else self._clock()) + max(0.0, float(within_s))
        # ⚠️ 不含 `locked`：那是「等條件」（`wait_until`），預定時刻到了也還不能開工；
        #    條件到了 `unlock_due` 會把它放回 pending，那時才算到期（2026-11-03 review）
        rows = [item for item in self._items
                if float(item.get("due_at") or 0.0) > 0.0
                and float(item.get("due_at") or 0.0) <= moment
                and str(item.get("state") or "") in ("pending", "hanging")]
        rows.sort(key=lambda item: (float(item.get("due_at") or 0.0),
                                    -float(item.get("priority") or 0)))
        return [dict(item) for item in rows]

    def next_task(self, now: float | None = None) -> dict | None:
        """下一個要做的：**先看預定時刻到了的**，再續 hanging，再 pending（優先度高的先）。"""
        moment = float(now if now is not None else self._clock())
        with self._writing():
            self.unlock_due(moment)
            self.mark_stale(moment)
            due = self.due_tasks(moment)
            if due:
                return due[0]
            candidates = [item for item in self._items
                          if str(item.get("state") or "") in PICK_ORDER]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (PICK_ORDER[str(item.get("state"))],
                                          -float(item.get("priority") or 0),
                                          float(item.get("updated_at") or 0)))
        return dict(candidates[0])

    # ── Activity 的生命週期掛勾 ────────────────────────────────────────────────
    def on_start(self, activity) -> None:
        """活動真的開工了 ⇒ 對應任務 processing，並記下是哪個活動在做（`activity_id`）。

        ⚠️ 2026-11-03 review：
          · `activity_id` 以前只在 `on_interrupt` 寫 ⇒ 開機對帳看到的正在跑的任務 activity_id 都是空的，
            會被當殭屍退回 hanging；
          · `add()` 現在會回收同一個 preset 還沒做完的那筆（可能是 pending／hanging）⇒ 一律再走一次
            `set_state("processing")`，不然活動在跑、看板卻寫「これから」，嘗試次數也不會加。
        """
        meta = getattr(activity, "meta", {}) or {}
        activity_id = str(getattr(activity, "id", "") or "")
        task_id = str(meta.get("task_id") or "")
        with self._writing():
            if not task_id:
                # 沒有對應任務（手動指定／舊存檔）⇒ 補登一筆（或回收同 preset 沒做完的那筆）
                item = self.add(str(getattr(activity, "title", "") or "（無題）"),
                                preset=str(meta.get("preset") or ""), state="pending")
                task_id = str(item["id"])
                activity.meta["task_id"] = task_id
            self.set_state(task_id, "processing", note="")
            if activity_id:
                self.patch(task_id, activity_id=activity_id)

    def on_interrupt(self, activity) -> None:
        """被主人打斷 ⇒ hanging（不是消失）。"""
        task_id = str((getattr(activity, "meta", {}) or {}).get("task_id") or "")
        if not task_id:
            return
        with self._writing():
            self.set_state(task_id, "hanging",
                           note=str((getattr(activity, "meta", {}) or {})
                                    .get("resume", {}).get("last") or "")[:120])
            # `steps`＝這件事一共幾步：App 的看板要寫「第 n／total 步起」（PLAN-UNITY-TASKS §5）
            self.patch(task_id, cursor=int(getattr(activity, "cursor", 0) or 0),
                       steps=len(getattr(activity, "steps", ()) or ()),
                       activity_id=str(getattr(activity, "id", "") or ""))

    def on_done(self, activity) -> None:
        task_id = str((getattr(activity, "meta", {}) or {}).get("task_id") or "")
        if task_id:
            self.set_state(task_id, "completed", note="")

    def on_failed(self, activity) -> None:
        """這一輪沒做成：回到 hanging，並把失敗次數 +1（≥2 次之後她會被要求寫技能）。"""
        task_id = str((getattr(activity, "meta", {}) or {}).get("task_id") or "")
        if not task_id:
            return
        with self._writing():
            item = self.get(task_id) or {}
            self.patch(task_id, state="hanging", note="この回はうまくいかなかった",
                       failures=int(item.get("failures") or 0) + 1)

    def failures_of(self, task_id: str) -> int:
        """這個任務已經失敗幾次（App／prompt 都用這個數）。"""
        return int((self.get(task_id) or {}).get("failures") or 0)

    def reconcile(self, active_activity_ids=(), active_task_ids=()) -> int:
        """開機對帳：`processing` 但對應的活動已經不存在 ⇒ 退回 `hanging`（owner 2026-11-03）。

        從前活動被輪換掉或被打斷，`_finish` 沒走到，那筆就一直掛在 `processing`——
        看板上會看到「同一個標題三筆都在進行中」這種殭屍。活動本身已經不在 store 裡，
        所以退回 `hanging`（她還記得自己做到哪，下次可以續做），不直接判死。
        """
        alive = {str(item) for item in (active_activity_ids or ()) if str(item)}
        # 正在跑的活動身上帶的 task_id 也算活著：`activity_id` 以前只在被打斷時才寫，
        # 舊資料裡正在做的那筆 activity_id 是空的（2026-11-03 review 實測會被誤回收）
        live_tasks = {str(item) for item in (active_task_ids or ()) if str(item)}
        fixed = 0
        with self._writing():
            for item in self._items:
                if str(item.get("state") or "") != "processing":
                    continue
                if str(item.get("activity_id") or "") in alive or str(item.get("id")) in live_tasks:
                    continue
                item["state"] = "hanging"
                item["note"] = (str(item.get("note") or "") or "再開機時這一步沒接上（已回收）")[:120]
                item["updated_at"] = self._clock()
                fixed += 1
            if fixed:
                self._save()
        return fixed


def _day(at: float) -> str:
    """`due_at` → `YYYY-MM-DD`（本地時區；沒有就空字串）。"""
    if not at:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(float(at)))


def _brief(item: dict) -> dict:
    return {"id": item.get("id"), "title": item.get("title"),
            "state": item.get("state"), "note": item.get("note") or ""}
