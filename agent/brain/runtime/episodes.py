"""自動切片／歸檔的執行端：**沒有任何「新會話」入口**。

`IngressRuntime` 在每一輪開始前問一句「該封片了嗎」；要封就封，然後把 `Episode` 交給
`on_seal` 回呼（未來的 `episode.summarize` → `memory.write` 管線）。使用者只看得到對話，
看不到切片——這正是要求的效果。

封片是**非阻塞**的：這裡只做記憶體搬移與回呼，摘要／抽取事實／寫 Hindsight 由背景管線做。
"""

from __future__ import annotations

import json
import pathlib
import threading
from dataclasses import replace
from typing import Callable

from agent.brain.domain.episode import CutReason, Episode, SegmentPolicy, cut_reason, seal
from agent.brain.domain.thread import Thread
from agent.brain.runtime.memory.pointer import make as make_pointer
from agent.brain.runtime.ops.rotation import DailyGuard, RotationGuard

#: 記憶體裡留幾筆封片。舊值 512 會把歷史擠出記憶體（2026-10-27 修好 id 後實測
#: `episodes.jsonl` 有 564 筆，512 一裁就少 52 筆）⇒ 預設拉到 2048；
#: 真的要吃滿（一輪一封的長期運行）再用 `BLUEAGENT_EPISODES_MAX` 調。
DEFAULT_MAX_KEPT = 2048


class EpisodeArchive:
    def __init__(self, *, policy: SegmentPolicy | None = None,
                 sink: Callable[[Episode], None] | None = None,
                 summarize: Callable[[Episode], str] | None = None,
                 window_summary: Callable[[object, float, float], str] | None = None,
                 max_kept: int = DEFAULT_MAX_KEPT, path=None) -> None:
        self._policy = policy or SegmentPolicy()
        self._sink = sink
        self._summarize = summarize
        #: 優先用「提煉窗口摘要」當承接段（owner 2026-10-23：她換段要接得上，
        #: 而且要是**壓縮過的**「這段時間講了什麼」，不是把前幾句原話串起來）。
        self._window_summary = window_summary
        self._max = max(16, int(max_kept))
        self._lock = threading.RLock()
        self._episodes: list[Episode] = []
        self.path = pathlib.Path(path) if path is not None else None
        #: 封片檔案也是 append-only ⇒ 一樣要上限（一輪一封，會慢慢長）。
        self._guard = RotationGuard(self.path) if self.path is not None else None
        # 分級輪轉：收片摘要按天切，保留 90 天（比 attention 長——它是記憶的旁證）
        self._daily = DailyGuard(self.path, kind="episodes") \
            if self.path is not None else None
        if self.path is not None:
            self._load()

    @property
    def policy(self) -> SegmentPolicy:
        return self._policy

    def cut_if_needed(self, thread: Thread, now: float, *, incoming: str = "",
                      topic_changed: bool = False, last_active: float | None = None) -> CutReason | None:
        with thread.lock:
            return cut_reason(thread, now, incoming=incoming, policy=self._policy,
                              topic_changed=topic_changed,
                              last_active=last_active)

    def seal(self, thread: Thread, now: float, reason: CutReason) -> Episode | None:
        with thread.lock:
            episode = seal(thread, now, reason)
        if episode is None:
            return None
        summary = ""
        if self._window_summary is not None:
            try:
                summary = str(self._window_summary(thread.key, episode.started_at,
                                                   episode.ended_at) or "").strip()
            except Exception:                     # noqa: BLE001 - 窗口摘要失敗退回逐句
                summary = ""
        if not summary and self._summarize is not None:
            try:
                summary = str(self._summarize(episode) or "").strip()
            except Exception:                     # noqa: BLE001 - 摘要失敗不擋歸檔
                summary = ""
        if summary:
            # 門牌（2026-10-25）：承接段是**壓縮產物**，第一行必須能回到原文那幾輪。
            # 沒有門牌的摘要只是轉述；有了它，下一段（或重啟後）才有辦法核對。
            ptr = make_pointer(episode.entity,
                               [str(turn.turn_id) for turn in episode.turns])
            carrier = f"〔{ptr}〕\n{summary}" if ptr else summary
            episode = replace(episode, summary=carrier)
            # **承接段**：把摘要寫回線，下一段（或下一個世代）才接得上。
            # 2026-10-22 之前這裡只放進 Episode，`Thread.carry_summary` 永遠是空的
            # ⇒ 換段就等於失憶。現在優先寫「提煉窗口摘要」（壓縮過的這段時間講了什麼）。
            # 2000 字的截斷留給尾巴：門牌在最前面，不會被切掉。
            with thread.lock:
                thread.carry_summary = carrier[:2000]
        with self._lock:
            self._episodes.append(episode)
            if len(self._episodes) > self._max:
                del self._episodes[: len(self._episodes) - self._max]
            self._append_locked(episode)
        if self._sink is not None:
            # 回呼只負責「排進背景管線」；封片本身不等待摘要，否則會拖慢下一句話。
            try:
                self._sink(episode)
            except Exception:                     # noqa: BLE001 - 歸檔失敗不該擋對話
                pass
        return episode

    def all(self) -> tuple[Episode, ...]:
        with self._lock:
            return tuple(self._episodes)

    def latest(self, count: int = 5) -> tuple[Episode, ...]:
        with self._lock:
            return tuple(self._episodes[-max(1, int(count)):])

    def reconcile_indexes(self, threads) -> dict:
        """把每條線的 `episode_index` 抬到「封片檔裡已有的最大序號」。

        為什麼要：序號是**每條線自己的**、封片當下只寫進 `episodes.jsonl`，
        要等這一輪結束 `threads.persist()` 才落進 `threads.json` ⇒ 中間崩潰的話，
        重啟後同一條線會再用一次同一個序號（id 已由開始時刻短哈希分開，但 `index`
        會重號：兩集都寫「第 15 集」）。啟動時對一次帳，之後序號只往前。

        回 `{"threads": 檔案裡有幾條線, "bumped": 抬了幾條}`。
        """
        top: dict[str, int] = {}
        for episode in self._episodes:
            top[episode.thread_id] = max(top.get(episode.thread_id, 0), episode.index)
        bumped = 0
        for thread in threads:
            want = top.get(getattr(thread, "id", ""), 0)
            if want > int(getattr(thread, "episode_index", 0) or 0):
                thread.episode_index = want
                bumped += 1
        return {"threads": len(top), "bumped": bumped}

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [{"id": ep.id, "entity": ep.entity, "index": ep.index,
                     "reason": ep.reason.value, "turns": len(ep.turns), "chars": ep.chars,
                     "summary_chars": len(ep.summary)} for ep in self._episodes[-20:]]

    def clear(self) -> None:
        with self._lock:
            self._episodes = []
            if self.path is not None:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _append_locked(self, episode: Episode) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(episode.to_dict(), ensure_ascii=False) + "\n")
            if self._guard is not None:
                self._guard.note_write()
            if self._daily is not None:
                self._daily.note_write()
        except OSError:
            pass

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return
        # 去重不能用 id 單鍵：2026-10-27 之前的 id 是 `ep_<entity>-<線內序號>`，每條線的第 1 集
        # 都叫 `ep_mira-1`（實測同一份檔裡 `ep_mira-1` 出現 63 次）⇒ 按 id 去重會把
        # 583 筆吃成 76 筆。改成（id, 線, 開始時刻）當鍵：同一集被改寫（ended_at 變了）時
        # **留最後一版**，不同線／不同時段的集數各自留下。
        seen: dict[tuple[str, str, float], int] = {}
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    continue
                episode = Episode.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
            if not episode.id:
                continue
            key = (episode.id, episode.thread_id, episode.started_at)
            place = seen.get(key)
            if place is None:
                seen[key] = len(self._episodes)
                self._episodes.append(episode)
            else:
                self._episodes[place] = episode          # 改寫版蓋掉舊版，順序不動
        if len(self._episodes) > self._max:
            del self._episodes[: len(self._episodes) - self._max]
