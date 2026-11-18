"""她的每日自我總結（owner 2026-10-24：「四個都做」的第 C 件）。

為什麼要有：她的活動日誌以前只是流水帳（一行「完成了：X」），做完就散掉；
一天下來沒有任何「我這一天過得怎樣」的東西。這裡一天一次，把**她的活動＋她說過的話**
收成三行日記，寫進 `data/notes/<entity>/reflection-<日期>.md` 並沉一條記憶事實
（label=她的日記，14 天內有效）——這樣她才有連續的「最近在過什麼日子」。

設計原則（跟其他背景工作一致）：
  · 一天只寫一次（檔案存在就跳過）⇒ 重啟不會重複寫
  · 沒有素材就不寫（不編造）
  · 失敗不回滾、不拋例外（背景工作不能影響對話）
  · **守披露層**（2026-10-24 review）：素材只取 public 活動＋主人在場的那幾條線；
    日記事實的房間＝素材房間的交集。以前跨線取最近 10 輪、存成 `audience=None`
    ⇒ 客人 1:1 說的秘密被洗成世界事實，換另一個客人在場也召回得到。
"""

from __future__ import annotations

import datetime
import pathlib
import threading
import time

from agent.brain.runtime.life import REFLECT_PROMPT
from agent.brain.runtime.activity_log import PUBLIC


class DailyReflection:
    """一天一次：把活動與對話收成三行日記。"""

    def __init__(self, *, root, memory, activity_log=None, raw_log=None, call=None,
                 entities=(), clock=time.time, lookback_h: float = 24.0,
                 max_activities: int = 12, max_turns: int = 10,
                 log=None, host: str = "owner") -> None:
        self.root = pathlib.Path(root)
        self.memory = memory
        self.activity_log = activity_log
        self.raw_log = raw_log
        self.call = call
        self.entities = tuple(str(entity) for entity in (entities or ()))
        self._clock = clock
        self.lookback_h = float(lookback_h)
        self.max_activities = int(max_activities)
        self.max_turns = int(max_turns)
        self._log = log or (lambda _message: None)
        #: 日記是「她跟主人的日子」：只收主人在場的線（客人的 1:1 不進日記）
        self.host = str(host or "owner")
        self.last: dict = {}
        self._lock = threading.RLock()

    # ── 素材 ────────────────────────────────────────────────────────────────
    def _material(self, entity: str) -> tuple[str, frozenset | None, list[str]]:
        """回 `(素材, 房間)`。房間 None＝只有 public 活動（世界可見）。

        · 活動只取 **public**：private 的是「只有她自己看得到」的私記，不能寫進主人聽得到的日記；
        · 原文只取**主人在場**的線；房間取交集（群聊那段也算，但客人不在的時候就說不出口）。
        """
        lines: list[str] = []
        works: list[str] = []          # 今天寫出來的**作品**（不算經歷，owner 2026-11-16）
        room: frozenset | None = None
        if self.activity_log is not None:
            try:
                rows = self.activity_log.entries(entity, limit=self.max_activities,
                                                 visibility=PUBLIC)
            except Exception:                               # noqa: BLE001
                rows = []
            for row in rows:
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                if int(row.get("fiction") or 0):
                    # 作品＝她寫的東西，不是她經歷的事：只留下「寫過什麼」給日記提一句
                    title = str(row.get("activity") or "").strip()
                    if title and title not in works:
                        works.append(title[:40])
                    continue
                lines.append(f"（わたしがやったこと）{text[:120]}")
        if self.raw_log is not None:
            try:
                pool = self.raw_log.recent(entity, limit=max(200, self.max_turns))
            except Exception:                               # noqa: BLE001
                pool = []
            wanted = {str(entity), self.host}
            turns = [row for row in pool
                     if wanted <= {str(who) for who in (row.get("room") or ())}]
            turns = turns[-self.max_turns:]
            for row in turns:
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                # 說話者標籤也是 prompt（日語；review 2026-10-28：以前是「我／對方」）
                who = "わたし" if str(row.get("role") or "") == "assistant" else str(
                    row.get("speaker") or row.get("role") or "相手")
                lines.append(f"（{who}）{text[:120]}")
                here = frozenset(str(person) for person in row.get("room") or ())
                room = here if room is None else room & here
        if works:
            lines.append("（今日、わたしが書いたもの＝作品。経験ではない）" + "／".join(works))
        return "\n".join(lines), room, works

    def _path(self, entity: str, day: str) -> pathlib.Path:
        return self.root / str(entity) / f"reflection-{day}.md"

    # ── 跑一次 ──────────────────────────────────────────────────────────────
    def reflect_once(self, entity: str) -> dict:
        if self.call is None:
            return {"ok": False, "reason": "not_configured"}
        now = float(self._clock())
        day = datetime.date.fromtimestamp(now).isoformat()
        path = self._path(entity, day)
        if path.exists():
            return {"ok": False, "reason": "already_today"}
        material, room, works = self._material(entity)
        if not material.strip():
            return {"ok": False, "reason": "no_material"}
        prompt = REFLECT_PROMPT.format(days="今日", material=material)
        try:
            text = str(self.call(prompt) or "").strip()[:500]
        except Exception as exc:                            # noqa: BLE001 - 背景失敗不影響對話
            self.last = {"ok": False, "reason": f"{type(exc).__name__}"}
            return self.last
        if not text:
            self.last = {"ok": False, "reason": "empty"}
            return self.last
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {day} 日記\n\n{text}\n", encoding="utf-8")
        except Exception as exc:                            # noqa: BLE001
            self._log(f"reflection: 寫檔案失敗（{type(exc).__name__}）")
        try:
            self.memory.add(text, layer="self", label="她的日記", self_owned=True,  # lang-ok: 存檔鍵
                            audience=None if room is None else set(room),
                            entity=str(entity), source="reflection", kind="event",
                            valid_from=now, valid_to=now + 14 * 86400)
        except Exception as exc:                            # noqa: BLE001
            self._log(f"reflection: 記憶沒寫成（{type(exc).__name__}）")
        if works:
            # 作品單獨記一條（fiction=1）：可以聊「我寫到港口那段」，不能當「我今晚在港口」
            try:
                self.memory.add("今日書いた作品：" + "／".join(works), layer="self",
                                label="她的作品", self_owned=True,  # lang-ok: 存檔鍵
                                entity=str(entity), source="reflection_work", kind="event",
                                valid_from=now, valid_to=now + 14 * 86400, fiction=1)
            except Exception as exc:                       # noqa: BLE001
                self._log(f"reflection: 作品沒進記憶（{type(exc).__name__}）")
        self.last = {"ok": True, "entity": str(entity), "day": day, "chars": len(text)}
        return self.last

    def tick(self) -> list[dict]:
        """所有角色各試一次（今天寫過就跳過）。"""
        results = []
        with self._lock:
            for entity in self.entities:
                try:
                    results.append(self.reflect_once(entity))
                except Exception as exc:                    # noqa: BLE001
                    results.append({"ok": False, "entity": entity,
                                    "reason": f"{type(exc).__name__}"})
        return results


def start_worker(reflection: DailyReflection, *, tick_s: float = 3600.0) -> threading.Thread:
    """背景執行緒：每小時看一次「今天寫了沒」（一天只會真的寫一次）。"""
    def loop() -> None:
        while True:
            time.sleep(max(60.0, float(tick_s)))
            try:
                reflection.tick()
            except Exception:                               # noqa: BLE001
                continue

    thread = threading.Thread(target=loop, name="reflection", daemon=True)
    thread.start()
    return thread
