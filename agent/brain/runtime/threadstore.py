"""線的存放區：**只負責「哪條線是活的」**，不碰記憶、不碰執行。

`attach()` 是自動連續的核心：
  · 同一組 key（principal, entity, scene, purpose）在 `window` 內 → 回同一條線；
  · 過窗 → 同一個 `base` 的下一世代（新的 thread id），舊線留給 reaper 收；
  · 因此客戶端不需要送、也不能送「幫我開新會話」這種請求。
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from agent.brain.domain.ids import ThreadKey
from agent.brain.domain.thread import Thread, ThreadState
from agent.brain.domain.event import SourceClass
from agent.brain.ports import ThreadAttachment
from agent.brain.runtime.ops.rotation import append_archive

#: 每條線在記憶體裡留幾條「當前片段」（`transcript` 自己已是 deque(maxlen=40)，
#: 這裡管 `segment`——封片前沒有上限，它才是 `threads.json` 變大的真正原因）。
DEFAULT_MAX_SEGMENT = 200
#: `threads.json` 超過這個大小 ⇒ 載入時先做一次封存再載（不是輪轉，檔案不能切半）。
DEFAULT_MAX_STATE_BYTES = 8 * 1024 * 1024


#: 跨線餘溫（`ThreadStore.elsewhere`）看多久以內的別條線；桌與時鐘的輪次不算「剛剛聊過」。
ELSEWHERE_WINDOW_S = 15 * 60.0
_ELSEWHERE_SKIP = ("trpg", "table", "clock:", "ambient")

#: 回收（E4，2026-11-17）：被新世代取代的舊線閒過這麼久就關。
#: 為什麼不是 15 分鐘就關：心跳的「主人最後說過什麼」（`recent_owner_lines`）跨線找，新世代剛開時
#: 只有一兩句，前幾句還在舊世代裡；幾個小時後那些話已經不算「剛剛」。
SUPERSEDED_AFTER_S = 6 * 3600.0
#: 當世代（某組 key 目前的那條線）整整這麼久沒人碰才關：它是下一世代承接摘要的來源，
#: 關掉＝下次換世代就接不上；只收真的死掉的（舊桌、舊語音房、不再來的客人）。
STALE_AFTER_S = 30 * 86400.0

#: 相容舊名；契約本體在 ports，Router 不再依賴這個具體存儲。
Attach = ThreadAttachment


class ThreadStore:
    def __init__(self, *, max_threads: int = 1024, path=None,
                 max_segment: int = DEFAULT_MAX_SEGMENT,
                 max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
                 archive_dir=None, raw_log=None) -> None:
        self._lock = threading.RLock()
        #: 寫檔鎖：序列化與寫檔在 `_lock` 外做（E4），這把只保證「舊快照不蓋過新快照」。
        #: ⚠️ 拿著它時**不准**再拿 `_lock`（反過來可以）——否則跟持 `_lock` 呼叫 persist 的路徑互鎖。
        self._write_lock = threading.Lock()
        self._snap_seq = 0
        self._written_seq = 0
        self._threads: dict[str, Thread] = {}
        self._current: dict[str, str] = {}        # key.base → 當世代 thread id
        self._gen: dict[str, int] = {}            # key.base → 已開過幾代
        self._max = max(16, int(max_threads))
        self._path = pathlib.Path(path) if path is not None else None
        self._max_segment = max(20, int(max_segment))
        self._max_state_bytes = max(0, int(max_state_bytes))
        #: 封存目錄：預設 `data/archive/`（與 `threads.json` 同層）。
        self._archive_dir = (pathlib.Path(archive_dir) if archive_dir is not None
                             else (self._path.parent / "archive" if self._path is not None else None))
        #: 原文倉（`runtime/rawlog.py`）：每輪追加一份，12 個月保留（owner 2026-10-23）
        self._raw_log = raw_log
        #: 累計被封存的 turn 數（`/api/metrics` 用）。
        self.archived = 0
        #: 累計被 `prune()` 收掉的線。
        self.pruned = 0
        #: 換世代時先對舊線呼叫一次（組合根接 `EpisodeArchive.seal`，見 `on_supersede`）。
        self._supersede_hook = None
        if self._path is not None:
            self._load()

    def get(self, thread_id: str) -> Thread | None:
        if not thread_id:
            return None
        with self._lock:
            thread = self._threads.get(str(thread_id))
            if thread is None or thread.state is ThreadState.CLOSED:
                return None
            return thread

    def attach(self, key: ThreadKey, *, now: float, window: float, lane: SourceClass) -> Attach:
        with self._lock:
            current = self._current.get(key.base)
            thread = self._threads.get(current) if current else None
            if (thread is not None and thread.state is not ThreadState.CLOSED
                    and now - thread.last_active_at <= max(0.0, window)):
                previous = thread.last_active_at
                thread.lane = min(thread.lane, lane)
                with thread.lock:
                    thread.touch(now)
                return Attach(thread=thread, created=False, previous_active=previous)

            generation = self._gen.get(key.base, 0) + 1
            self._gen[key.base] = generation
            fresh = Thread(key=key, id=key.with_generation(generation), lane=lane,
                           created_at=now, last_active_at=now)
            # 換世代（過窗／新一段）時**繼承承接摘要**：淡化 session 的前提是
            # 「下一段還記得上一段」，不是「時間到就失憶」。
            previous_thread = self._threads.get(current) if current else None
            if previous_thread is not None:
                self._seal_superseded_locked(previous_thread)
                with previous_thread.lock:
                    fresh.carry_summary = str(previous_thread.carry_summary or "")
            self._threads[fresh.id] = fresh
            self._current[key.base] = fresh.id
            self._evict_locked()
        # 鎖外寫檔：開新線那一下不該讓同時進來的其他訊息等序列化
        self.persist()
        return Attach(thread=fresh, created=True, previous_active=now)

    def on_supersede(self, hook) -> None:
        """換世代那一刻先把舊線還沒封片的尾巴封掉，承接摘要才接得到最近那一段（2026-11-17）。

        以前：封片只在**同一條線**下一句進來時判（`IngressRuntime._maybe_seal`）；過窗（15 分鐘）
        就直接開新世代，舊線最後那一段（最多 6 輪）從來沒封 ⇒ 新世代繼承的 `carry_summary`
        停在**再前一段**——隔一會兒再聊，她記得早一點的事，偏偏漏了剛才那幾句。
        `hook(previous_thread)` 由組合根接（`EpisodeArchive.seal`，會把摘要寫回舊線），
        在 `_lock` 裡跑、只在換世代時跑一次；失敗不擋開新線。
        """
        self._supersede_hook = hook

    def _seal_superseded_locked(self, previous: Thread) -> None:
        if self._supersede_hook is None or not previous.segment:
            return
        if previous.inflight or previous.queued:
            return                                  # 還在記輪次，留給下一次
        try:
            self._supersede_hook(previous)
        except Exception:                          # noqa: BLE001 - 封片壞了不擋開新線
            pass

    def close(self, thread_id: str) -> Thread | None:
        with self._lock:
            thread = self._threads.get(str(thread_id))
            if thread is None:
                return None
            thread.state = ThreadState.CLOSED
            if self._current.get(thread.key.base) == thread.id:
                self._current.pop(thread.key.base, None)
            self._threads.pop(thread.id, None)
        self.persist()
        return thread

    def prune(self, now: float, *, superseded_after: float = SUPERSEDED_AFTER_S,
              stale_after: float = STALE_AFTER_S) -> list[Thread]:
        """收掉不會再用的線（E4，2026-11-17；呼叫端：`runtime/ops/thread_reaper.py`）。

        線上實測 513 條活線：`reap()` 從第一版起就沒有人呼叫，過窗開的新世代把舊世代留在這裡，
        要等到 `max_threads`（1024）才被擠掉 ⇒ `threads.json` 只會線性變大。

        · 舊世代（不是該 key 的當世代）：閒過 `superseded_after` 就關；
        · 當世代：閒過 `stale_after` 才關（承接摘要的來源，見 `STALE_AFTER_S`）；
        · 正在跑／有排隊的絕不關。
        關之前把還沒進原文倉的輪次補進去（關了之後 persist 就看不到它了）。
        回傳被關的線——片段尾巴要不要封片是呼叫端的事。
        """
        closed: list[Thread] = []
        with self._lock:
            for thread in list(self._threads.values()):
                if thread.inflight or thread.queued:
                    continue
                current = self._current.get(thread.key.base) == thread.id
                idle = float(now) - float(thread.last_active_at or 0.0)
                if idle < (stale_after if current else superseded_after):
                    continue
                if self._raw_log is not None:
                    try:
                        self._raw_log.sync_thread(thread)
                    except Exception:              # noqa: BLE001 - raw 壞了不擋回收
                        pass
                thread.state = ThreadState.CLOSED
                if current:
                    self._current.pop(thread.key.base, None)
                self._threads.pop(thread.id, None)
                closed.append(thread)
            self.pruned += len(closed)
        if closed:
            self.persist()
        return closed

    def reap(self, now: float, *, idle_after: float = 20 * 60.0,
             close_after: float = 45 * 60.0) -> list[Thread]:
        """過窗的線：先轉 IDLE，再 CLOSED。回傳這次被關掉的（呼叫端拿去封片）。"""
        closed: list[Thread] = []
        with self._lock:
            for thread in list(self._threads.values()):
                if thread.inflight or thread.queued:
                    continue
                idle = now - thread.last_active_at
                if idle >= close_after:
                    thread.state = ThreadState.CLOSED
                    if self._current.get(thread.key.base) == thread.id:
                        self._current.pop(thread.key.base, None)
                    self._threads.pop(thread.id, None)
                    closed.append(thread)
                elif idle >= idle_after and thread.state is ThreadState.ACTIVE:
                    thread.state = ThreadState.IDLE
        if closed:
            self.persist()
        return closed

    def current_for(self, key: ThreadKey) -> Thread | None:
        with self._lock:
            return self._threads.get(self._current.get(key.base, ""))

    def threads(self) -> tuple[Thread, ...]:
        with self._lock:
            return tuple(self._threads.values())

    def recent_dialogue(self, entity: str, limit: int = 12) -> list[dict]:
        """該角色**最近一條活線**的 transcript 尾巴（時間序，`speaker`／`role`／`text`／`at`）。

        為什麼要有：心跳台詞的「主人最後說過什麼」以前只讀 `raw` 的最後 6 行（＝3 輪），
        而 raw 本身可能整段沒落（2026-11-01 nori：11:29–11:45 九輪全缺，主人講的
        「池袋あたりに電器店…」就在裡面）⇒ 她只記得「午後は出る」，於是反覆問「どこへ」。
        transcript 是**當前窗口的全量**，比 raw 的固定條數更貼近「剛剛聊到哪」。
        """
        entity = str(entity or "")
        if not entity:
            return []
        with self._lock:
            best = None
            for thread in self._threads.values():
                if str(getattr(thread.key, "entity", "")) != entity:
                    continue
                if not thread.transcript:
                    continue
                if best is None or float(thread.last_active_at or 0.0) > float(best.last_active_at or 0.0):
                    best = thread
            if best is None:
                return []
            rows = list(best.transcript)[-max(1, int(limit)):]
            return [{"speaker": turn.speaker, "role": turn.role, "text": turn.text,
                     "source": turn.source, "at": turn.at} for turn in rows]

    def recent_owner_lines(self, entity: str, limit: int = 3, *,
                           owners: tuple[str, ...] = ("owner",)) -> list[dict]:
        """**主人**最近真的說過的話——跨所有線，按時間取最後幾條。

        為什麼不能沿用 `recent_dialogue`：它取「最近一條活線」，那條線可能是
        **角色↔角色**的線（實測 2026-11-08：aster 的最近活線是跟ミラ互刷「……う」，
        說話人是 `mira`）⇒ 主人那一側被整段過濾掉，心跳的「主人最後說過什麼」
        變成空的，她只剩自己上一句可以換詞（連五次「オーナーの番」）。

        `owners`：主人在線上的說話人 id（只有 owner 是 master；App 換帳號登入時
        說話人會是那個帳號的 principal，那種情況這裡照樣看不到——這是身分層的事，
        不是這支的責任）。
        """
        entity = str(entity or "")
        if not entity:
            return []
        wanted = {str(item) for item in owners if str(item).strip()} or {"owner"}
        rows: list[dict] = []
        with self._lock:
            for thread in self._threads.values():
                if str(getattr(thread.key, "entity", "")) != entity:
                    continue
                # 桌（TRPG）那幾條線：輪次在資料形狀上是 role=user／speaker=owner，其實是桌的 prompt
                # （DIAGNOSIS 2026-11-10 B2：心跳的「主人最後說過什麼」拿到桌上的話）
                if str(getattr(thread.key, "purpose", "")) == "trpg":
                    continue
                for turn in thread.transcript:
                    text = str(turn.text or "").strip()
                    if not text or str(turn.role) != "user":
                        continue
                    if str(turn.source or "").lower().startswith(("trpg", "table")):
                        continue
                    if str(turn.speaker or "") not in wanted:
                        continue
                    rows.append({"speaker": turn.speaker, "role": turn.role, "text": text,
                                 "source": turn.source, "at": float(turn.at or 0.0)})
        rows.sort(key=lambda row: row["at"])
        return rows[-max(1, int(limit)):]

    def elsewhere(self, entity: str, *, current: str, present, now: float,
                  window_s: float = ELSEWHERE_WINDOW_S, per_thread: int = 3,
                  max_threads: int = 2) -> list[dict]:
        """跨線短期餘溫（D2，owner 2026-11-17「帶上餘溫，但要標渠道來源」）。

        她**別的活線**最近 `window_s` 內的最後幾輪：App 私聊剛聊完、轉頭到 Discord 語音，
        那條線還沒封片、蒸餾也還沒跑 ⇒ 不給這一段，這邊完全不知道剛剛在聊什麼。

        披露：**這一場的人 ⊆ 那條線出現過的人**（`present` ⊆ `participants`）才給——
        在場有誰沒參與那段，就整段不給（主人私聊的話不會跑到有客人的房間）。
        不收：這條線自己、她自己的線（`scope=self:*`）、桌（`purpose=trpg`）與桌／時鐘來源的輪次。
        回傳按時間新到舊，每段 `{source, group, at, rows:[{speaker, role, text, at}]}`。
        """
        entity = str(entity or "")
        wanted = {str(item) for item in (present or ()) if str(item)} - {entity}
        if not entity or not wanted:
            return []
        found: list[dict] = []
        with self._lock:
            for thread in self._threads.values():
                key = thread.key
                if (thread.id == str(current or "") or str(key.entity) != entity
                        or str(key.scope).startswith("self") or str(key.purpose) == "trpg"
                        or thread.state is ThreadState.CLOSED):
                    continue
                with thread.lock:
                    rows = [turn for turn in thread.transcript
                            if str(turn.text or "").strip()
                            and float(turn.at or 0.0) >= float(now) - float(window_s)
                            and not str(turn.source or "").lower().startswith(_ELSEWHERE_SKIP)]
                    seen = set(thread.participants) | {str(turn.speaker) for turn in thread.transcript
                                                       if str(turn.role) == "user" and turn.speaker}
                if not rows or not wanted <= seen:
                    continue
                tail = rows[-max(1, int(per_thread)):]
                found.append({
                    "source": str(tail[-1].source or ""),
                    "group": str(key.scope).startswith("group"),   # ThreadKey 把 `group:` 正規化成 `group-`
                    "at": float(tail[-1].at or 0.0),
                    "rows": [{"speaker": turn.speaker, "role": turn.role, "text": turn.text,
                              "source": str(turn.source or ""), "at": float(turn.at or 0.0)}
                             for turn in tail],
                })
        found.sort(key=lambda item: item["at"], reverse=True)
        return found[:max(0, int(max_threads))]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [thread.snapshot() for thread in self._threads.values()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._threads)

    def clear(self) -> None:
        with self._lock:
            # 已經拍好、還沒寫的快照作廢（不然清空之後又被寫回來）
            with self._write_lock:
                self._snap_seq += 1
                self._written_seq = self._snap_seq
            self._threads.clear()
            self._current.clear()
            self._gen.clear()
            if self._path is not None:
                try:
                    self._path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def persist(self) -> bool:
        """把当前活线原子写到磁盘；没有 path 时保持内存模式。

        E4（2026-11-17，線上 p95 31 ms）：`_lock` 裡只拍快照（dict），`json.dumps`＋寫檔在鎖外——
        以前整段持鎖，每輪結束那 30 ms 裡 Router 的 `attach()` 全部排隊。
        兩個 persist 同時跑時用序號保證「後拍的快照不會被先拍的蓋掉」。
        緊湊 JSON（不縮排）：檔案是機器讀的，`indent=1` 只多佔空間與時間。
        """
        if self._path is None:
            return False
        with self._lock:
            # ★ 先落 raw（原文倉），再封存超量 segment——順序反了就會丟掉被封存掉的那幾輪
            self._sync_raw_locked()
            # 寫之前先封存超量的 segment（不然檔案只會單向變大）
            self._archive_overflow_locked()
            body = {
                "threads": [thread.to_store_dict() for thread in self._threads.values()],
                "current": dict(self._current),
                "gen": dict(self._gen),
            }
            with self._write_lock:
                self._snap_seq += 1
                seq = self._snap_seq
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            if seq <= self._written_seq:
                return True                        # 更新的快照已經寫進去了
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(self._path)
                self._written_seq = seq
                return True
            except OSError:
                return False

    def _sync_raw_locked(self) -> int:
        """把每條線還沒進 raw 的輪次追加進 `data/raw/<entity>/`（失敗不擋存檔）。"""
        if self._raw_log is None:
            return 0
        written = 0
        for thread in list(self._threads.values()):
            try:
                written += int(self._raw_log.sync_thread(thread) or 0)
            except Exception:                      # noqa: BLE001 - raw 壞了不該擋對話
                continue
        return written

    def _archive_overflow_locked(self) -> int:
        """把每條線超過 `max_segment` 的 turn 搬進 `archive/threads-<YYYYMM>.jsonl`。

        為什麼搬 `segment` 而不是 `transcript`：`transcript` 是給 prompt 的滾動窗
        （deque 已有上限）；`segment` 是「當前片段全量」，封片前沒有上限——它就是
        `threads.json` 變大的真正原因。搬走不影響對話（prompt 只看 transcript），
        原始順序也保留在封存檔裡（append，不重排、不刪）。
        """
        if self._archive_dir is None or self._max_segment <= 0:
            return 0
        moved = 0
        for thread in list(self._threads.values()):
            with thread.lock:
                overflow = len(thread.segment) - self._max_segment
                if overflow <= 0:
                    continue
                rows = [{"archived_at": round(time.time(), 3), "thread": thread.id,
                         "entity": thread.entity, "key": thread.key.to_log(),
                         "turn": turn.to_dict()}
                        for turn in thread.segment[:overflow]]
                del thread.segment[:overflow]
            if append_archive(self._archive_dir, "threads", rows) is not None:
                moved += len(rows)
        self.archived += moved
        return moved

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return
        if not isinstance(raw, dict):
            return
        for row in raw.get("threads") or ():
            if not isinstance(row, dict):
                continue
            try:
                thread = Thread.from_store_dict(row)
            except (KeyError, TypeError, ValueError):
                continue
            if thread.state is ThreadState.CLOSED:
                continue
            self._threads[thread.id] = thread
            try:
                generation = int(thread.id.rsplit("-", 1)[-1])
            except ValueError:
                generation = 1
            self._gen[thread.key.base] = max(
                self._gen.get(thread.key.base, 0), generation)
        current = raw.get("current")
        if isinstance(current, dict):
            self._current = {str(key): str(value) for key, value in current.items()
                             if str(value) in self._threads}
        for thread in self._threads.values():
            if thread.key.base not in self._current:
                self._current[thread.key.base] = thread.id
        gen = raw.get("gen")
        if isinstance(gen, dict):
            for key, value in gen.items():
                try:
                    self._gen[str(key)] = max(self._gen.get(str(key), 0), int(value))
                except (TypeError, ValueError):
                    continue
        # 「載入時壓縮」：檔案已經超過上限 ⇒ 先把超量 segment 封存，再寫回小檔。
        # （不是輪轉——`threads.json` 是一個 JSON 物件，切半會壞掉。）
        try:
            oversized = (self._max_state_bytes > 0 and self._path is not None
                          and self._path.stat().st_size > self._max_state_bytes)
        except OSError:
            oversized = False
        if oversized and self._archive_overflow_locked():
            self.persist()

    def _evict_locked(self) -> None:
        if len(self._threads) <= self._max:
            return
        # 先淘汰 CLOSED / 最久沒動的，絕不淘汰正在跑的。
        candidates = [t for t in self._threads.values() if not t.inflight and not t.queued]
        candidates.sort(key=lambda t: (t.state is not ThreadState.CLOSED, t.last_active_at))
        for thread in candidates[: max(1, len(self._threads) - self._max)]:
            thread.state = ThreadState.CLOSED
            if self._current.get(thread.key.base) == thread.id:
                self._current.pop(thread.key.base, None)
            self._threads.pop(thread.id, None)
