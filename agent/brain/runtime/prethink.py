"""提前想（idle pre-think）：她一個人的時候先想，等下一輪直接用。

owner 2026-10-23：「模擬人類的思考」——人不是等人開口才開始想；走路、洗碗、發呆的時候
腦子已經在回放剛才的對話、預演等一下要說什麼。

所以這件事**不能佔她回話的時間**：

    閒下來（距上一輪 ≥ `idle_s`）→ 背景醒一次 → 讀最近幾輪原文 → 寫一段「內心的預想」
    → 落 `data/prethink/<entity>.json` → 下一輪由 `SubAgentStage` 當成筆記帶進 prompt

三條約束（跟子代理同一套規矩）：

1. **只讀**：不改記憶、不發訊息、不動狀態。
2. **有預算**：`max_age_s` 內只用一次（用過或過期就作廢）；每 `interval_s` 最多想一次。
3. **不擋主線**：整段在背景執行緒裡跑，失敗就當沒想過（`ok=False`），不影響回話。
4. **守披露層**（2026-10-24）：只回放**最近那一條線**，預想帶著那段對話的房間；
   取用時 `在場 ⊆ 房間` 才放行。以前是跨線取最近 12 輪、誰下一輪開口就給誰
   ⇒ 客人那段的預想會進主人的 prompt（反之亦然）。

2026-11-09 檢查後修正：
  · **素材沒變就不再想**：以前閒著時每 2 分鐘拿同一段對話再想一次、整夜不停（模擬：一個角色
    8 小時 239 次、素材只有 1 種），還把 `at` 刷新 ⇒ 30 分鐘過期永遠不生效；
  · **新不新鮮按那段對話的時間算**：昨晚的對話不能在今早當成「さっき」想的帶進來；
    對話本身比 `max_age_s` 舊就不拿它想（改想她自己的事）；
  · **睡著不想、預算不夠不想**（`asleep`／`budget` 由組合根接上）；
  · 活動素材只用**她自己的事**：角色之間的私聊（`kind=social`）不拿來想——那是兩人的房間，
    想出來卻標成「她＋主人」的房間，等於把別人的私聊端給主人；
  · 逐字稿標成「わたし／オーナー／名字」，不再是 `owner:`／`assistant:`（模型分不清哪個是自己）；
  · 一輪只掃一次原文（以前 `idle_for` 兩次＋`recent(200)` 一次，全量掃三遍）。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time

from agent.brain.runtime.memory.disclosure import disclosable

DEFAULT_IDLE_S = float(os.environ.get("BLUEAGENT_PRETHINK_IDLE_S") or 150)
DEFAULT_INTERVAL_S = float(os.environ.get("BLUEAGENT_PRETHINK_INTERVAL_S") or 120)
DEFAULT_MAX_AGE_S = float(os.environ.get("BLUEAGENT_PRETHINK_MAX_AGE_S") or 1800)
#: 活動素材不拿的種類：角色之間的私聊（兩人的房間，不是她一個人的事）
ACTIVITY_SKIP_KINDS = frozenset({"social"})

PROMPT = """いまは一人でいて、相手は何も言っていない。**心の中で**さっきのやりとりを思い返して、今の自分の考えを書いて：

① 相手のあの言葉、気にしていたのは／言い出せなかったのは何だろう？
② 次に相手が何を言ってきても、まずそれに応じる。そのうえで、話が合えば自分から触れたいことは？（一言でいい。台詞にはしない。言いそびれても構わない）
③ 聞きたいのにまだ聞けていないことは？

一人称で、話し言葉で、三文以内。**相手に話しかけない、台詞を書かない、箇条書きにしない**。

ここ最近のやりとり：
"""


class PreThinker:
    def __init__(self, *, raw_log=None, call=None, root=None,
                 entities=(), idle_s: float = DEFAULT_IDLE_S,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 max_age_s: float = DEFAULT_MAX_AGE_S, clock=time.time,
                 activity_log=None, asleep=None, budget=None, host: str = "owner",
                 names=None) -> None:
        self.raw_log = raw_log
        self.call = call
        self.root = pathlib.Path(root) if root is not None else None
        self.entities = [str(item) for item in entities if str(item).strip()]
        self.idle_s = max(30.0, float(idle_s))
        self.interval_s = max(30.0, float(interval_s))
        self.max_age_s = max(60.0, float(max_age_s))
        self._clock = clock
        #: 沒有對話可參考時，用「她自己在做的事」當素材（owner 2026-10-24 的第 D 件）
        self.activity_log = activity_log
        self._last_run: dict[str, float] = {}
        #: 上一次想的是哪一份素材（`basis`）：一樣就不再想
        self._basis: dict[str, str] = {}
        self.last: dict = {}
        #: `asleep(entity, now) -> bool`（生物鐘）／`budget.allows(entity, cost)`：組合根接上
        self.asleep = asleep
        self.budget = budget
        #: 主人的 principal（活動素材的房間、逐字稿的「オーナー」）
        self.host = str(host or "owner")
        #: principal／角色 id → 名字（逐字稿用）
        self.names = dict(names or {})

    def attach(self, *, asleep=None, budget=None, host: str | None = None, names=None) -> None:
        """組合根在生物鐘／預算／名字都建好之後接上（`compose/bootstrap.py`）。"""
        if asleep is not None:
            self.asleep = asleep
        if budget is not None:
            self.budget = budget
        if host:
            self.host = str(host)
        if names:
            self.names.update({str(key): str(value) for key, value in dict(names).items()})

    # ── 檔案 ────────────────────────────────────────────────────────────────
    def _path(self, entity: str) -> pathlib.Path | None:
        if self.root is None:
            return None
        return self.root / f"{entity}.json"

    def load(self, entity: str) -> dict | None:
        path = self._path(entity)
        if path is None:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self, entity: str, row: dict) -> None:
        path = self._path(entity)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def clear(self, entity: str) -> None:
        path = self._path(entity)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    # ── 取用（下一輪）────────────────────────────────────────────────────────
    def _fresh(self, entity: str) -> dict | None:
        row = self.load(entity)
        if not row:
            return None
        # 新不新鮮按**素材**（那段對話／那幾件事）的時間，不是按想的時間——
        # 以前閒著時一直重想、`at` 一直刷新 ⇒ 昨晚的對話今早還算「さっき」
        born = min(float(row.get("at") or 0), float(row.get("material_at") or row.get("at") or 0))
        age = float(self._clock()) - born
        if age > self.max_age_s:
            self.clear(entity)
            return None
        return row

    @staticmethod
    def _disclosable(entity: str, present, row: dict) -> bool:
        """`在場 ⊆ 那段對話的房間`；沒記房間（舊檔）⇒ 不放行（fail-closed，過期自然清掉）。

        規則本體在 `runtime/memory/disclosure.py`：檢索（`memory_search`）、回原文（`memory_open`）
        與這裡共用一份，不再各寫一段（2026-10-27 收斂）。
        """
        people = {str(who).strip() for who in (present or ()) if str(who).strip()}
        people.add(str(entity))
        return disclosable(people, row.get("room"))

    def take(self, entity: str, *, present) -> str:
        """拿一份「還新鮮」且**這幾個人面前說得出口**的預想（用過就作廢）。

        房間對不上就**不取用也不作廢**：那份預想留給對的人，過期自然清掉。
        """
        row = self._fresh(entity)
        if row is None or not self._disclosable(entity, present, row):
            return ""
        self.clear(entity)                     # 只用一次
        return str(row.get("note") or "").strip()

    def peek(self, entity: str, *, present) -> str:
        """只看不取（心跳接地用）：一樣要過披露層。"""
        row = self._fresh(entity)
        if row is None or not self._disclosable(entity, present, row):
            return ""
        return str(row.get("note") or "").strip()

    # ── 想一次 ──────────────────────────────────────────────────────────────
    def idle_for(self, entity: str) -> float:
        """距最近一輪過了多久（沒有原文就回無限大）。"""
        if self.raw_log is None:
            return float("inf")
        rows = self.raw_log.recent(entity, limit=1)
        if not rows:
            return float("inf")
        return float(self._clock()) - float(rows[-1].get("at") or 0)

    #: 沒有對話時的預想提示詞（素材＝她自己的活動）
    ACTIVITY_PROMPT = (
        "あなたはある役を演じていて、一人で自分のことをしている。下は{days}さっきまでやっていたこと。\n"
        "一人称で、今考えていることを書いて：① 一番響いたところ ② もっと突き止めたいこと ③ 誰かに話したいこと。\n"
        "二、三文、話し言葉で、箇条書きにしない、誰にも話しかけない。\n\n{material}\n"
    )

    def _label(self, entity: str, row: dict) -> str:
        """逐字稿的說話人：她自己＝わたし、主人＝オーナー、其他＝名字（沒有就 id）。"""
        who = str(row.get("speaker") or "").strip()
        if str(row.get("role") or "") == "assistant" or who == str(entity):
            return "わたし"
        if who == self.host:
            return "オーナー"
        return str(self.names.get(who) or who or "相手")

    def _gate(self, entity: str, now: float) -> str:
        """想之前的三道閘：太頻繁、睡著、預算不夠。回空字串＝可以想。"""
        if now - self._last_run.get(entity, 0.0) < self.interval_s:
            return "too_soon"
        if self.asleep is not None:
            try:
                if self.asleep(entity, now):
                    return "asleep"
            except Exception:                               # noqa: BLE001 - 生物鐘壞了不擋
                pass
        if self.budget is not None and hasattr(self.budget, "allows"):
            try:
                if not self.budget.allows(entity, 0.2):
                    return "no_budget"
            except Exception:                               # noqa: BLE001
                pass
        return ""

    def _same(self, entity: str, basis: str) -> bool:
        if basis == self._basis.get(entity):
            return True
        saved = self.load(entity) or {}
        return bool(basis) and basis == str(saved.get("basis") or "")

    def _write(self, entity: str, now: float, note: str, row: dict, basis: str) -> dict:
        self._basis[entity] = basis
        self._save(entity, {"entity": entity, "at": now, "note": note, "basis": basis, **row})
        self.last = {"ok": True, "entity": entity, "chars": len(note),
                     **({"source": row["source"]} if row.get("source") else {})}
        return self.last

    def _ask(self, prompt: str):
        """呼叫模型；回 `(note, failure)`。"""
        try:
            note = str(self.call(prompt) or "").strip()[:400]
        except Exception as exc:                            # noqa: BLE001 - 背景失敗不影響對話
            self.last = {"ok": False, "reason": f"{type(exc).__name__}"}
            return "", self.last
        if not note:
            self.last = {"ok": False, "reason": "empty"}
            return "", self.last
        return note, None

    def _think_from_activity(self, entity: str, now: float) -> dict:
        """沒有對話素材時，用她自己的活動想一次（owner 2026-10-24 第 D 件）。"""
        log = self.activity_log
        if log is None:
            return {"ok": False, "reason": "not_enough_turns"}
        try:
            rows = log.entries(entity, limit=12)
        except Exception:                                   # noqa: BLE001
            rows = []
        # 只用她自己的事：角色之間的私聊（兩人的房間）不拿來想
        rows = [row for row in rows if str(row.get("kind") or "") not in ACTIVITY_SKIP_KINDS
                and str(row.get("text") or "").strip()][-4:]
        if len(rows) < 2:
            return {"ok": False, "reason": "no_material"}
        material_at = max(float(row.get("at") or 0.0) for row in rows)
        if now - material_at > self.max_age_s:
            return {"ok": False, "reason": "stale"}         # 最近沒做什麼 ⇒ 不拿舊事硬想
        basis = "activity:" + "|".join(f"{row.get('at')}" for row in rows)
        if self._same(entity, basis):
            return {"ok": False, "reason": "same_material"}
        material = "\n".join(f"・{str(row.get('text')).strip()[:120]}" for row in rows)
        self._last_run[entity] = now
        note, failure = self._ask(self.ACTIVITY_PROMPT.format(days="", material=material))
        if failure:
            return failure
        return self._write(entity, now, note, {
            "thread": "", "room": sorted({str(entity), self.host}), "based_turns": [],
            "source": "activity", "material_at": material_at}, basis)

    def think_once(self, entity: str) -> dict:
        """跑一次預想。回 `{ok, note, reason}`——失敗不拋例外。"""
        if self.call is None:
            return {"ok": False, "reason": "not_configured"}
        now = float(self._clock())
        gate = self._gate(entity, now)
        if gate:
            return {"ok": False, "reason": gate}
        if self.raw_log is None:
            # 沒有原文倉（例如只接了活動日誌）⇒ 直接想「她自己的事」
            return self._think_from_activity(entity, now)
        # 桌（TRPG）喊她出手的那幾輪：原文裡記成 owner 說的，其實是桌的 prompt ⇒ 不當成對話來想
        pool = [row for row in self.raw_log.recent(entity, limit=200)      # 一輪只掃一次
                if not str(row.get("source") or "").lower().startswith(("trpg", "table"))]
        idle = now - float(pool[-1].get("at") or 0) if pool else float("inf")
        if idle < self.idle_s:
            return {"ok": False, "reason": "not_idle"}
        line = str(pool[-1].get("thread") or "") if pool else ""
        rows = [row for row in pool if str(row.get("thread") or "") == line][-12:]
        if len(rows) < 2 or idle > self.max_age_s:
            # owner 2026-10-24：沒有 2 輪對話也要有內心活動 ⇒ 想她自己在做的事。
            # 2026-11-09：對話本身太舊（比預想的保鮮期還舊）也一樣——昨晚的話不算「さっき」。
            return self._think_from_activity(entity, now)
        # 這段對話的房間＝每一輪房間的交集（最嚴）；不明就不想（想了也沒人能聽）
        rooms = [frozenset(str(who) for who in (row.get("room") or ())) for row in rows]
        room = frozenset.intersection(*rooms)
        if not room:
            return {"ok": False, "reason": "no_room"}
        basis = "talk:" + "|".join(str(row.get("turn") or row.get("at")) for row in rows)
        if self._same(entity, basis):
            return {"ok": False, "reason": "same_material"}   # 同一段話想過了 ⇒ 不重想
        transcript = "\n".join(f"{self._label(entity, row)}: {row.get('text')}" for row in rows)
        self._last_run[entity] = now
        note, failure = self._ask(PROMPT + transcript)
        if failure:
            return failure
        return self._write(entity, now, note, {
            "thread": rows[-1].get("thread"), "room": sorted(room),
            "based_turns": [str(item.get("turn")) for item in rows[-4:]],
            "material_at": float(rows[-1].get("at") or now), "idle_s": round(idle, 1)}, basis)


def start_worker(prethinker: PreThinker, *, tick_s: float = 60.0) -> threading.Thread:
    """背景執行緒：每 `tick_s` 看一次「有沒有誰閒著、該想一下了」。"""
    def loop() -> None:
        while True:
            time.sleep(max(20.0, float(tick_s)))
            for entity in prethinker.entities:
                try:
                    prethinker.think_once(entity)
                except Exception:                           # noqa: BLE001
                    continue
    thread = threading.Thread(target=loop, name="prethink", daemon=True)
    thread.start()
    return thread
