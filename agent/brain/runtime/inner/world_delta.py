"""「世界はどう変わったか」——兩層（owner 2026-11-03 拍板）。

共享層（一份給所有人）在 `world_shared.py`；這裡是 `WorldDelta`（每個角色自己那一份：
這段期間誰找過我、我自己留下了什麼）與它的取用工具。共享層的名字由本檔轉出，既有 import 不必改。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import time

from agent.brain.runtime.inner.world_shared import (  # noqa: F401 - 既有 import 的轉出口
    SHARED_KEYS, SharedWorld, _COUNT_KEYS, _LABELS, _gap_text, _now_parts,
    _part_of_day, coarse_location_digest)

class WorldDelta:
    """一個角色一份：**她**看到的世界差量（時間、台帳差、誰找過她、她留下的東西）。"""

    def __init__(self, root, entity: str, *, ledger=None, notes_root=None, talked=None,
                 shared: "SharedWorld | None" = None, events=None, activity_log=None,
                 peers=(), principal: str = "owner", guild_ids=(), table_ids=(),
                 clock=time.time, tz: str = "", max_chars: int = 600,
                 fresh_s: float = 300.0) -> None:
        self.root = pathlib.Path(root)
        self.entity = str(entity)
        self.ledger = ledger            # TaskLedger 或 None（沒有就不報台帳）
        self.notes_root = pathlib.Path(notes_root) if notes_root is not None else None
        self.talked = talked            # `(entity, since) -> (次數, 最後一句)` 或 None
        self.shared = shared            # SharedWorld 或 None
        self.events = events            # WorldEvents（環境與限定房間事件）
        self.activity_log = activity_log
        self.peers = tuple(str(item) for item in (peers or ()) if str(item) != self.entity)
        self.principal = str(principal or "owner")
        self.guild_ids = guild_ids
        self.table_ids = table_ids
        self._clock = clock
        self.tz = str(tz or "")
        self.max_chars = max(80, int(max_chars))
        self.fresh_s = max(30.0, float(fresh_s))

    # ── 落盤 ────────────────────────────────────────────────────────────────
    def path(self) -> pathlib.Path:
        return self.root / f"{self.entity}.json"

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, state: dict) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.path().write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass                        # 記不住上一次不是大事，不擋她做事

    # ── 三個觀測 ─────────────────────────────────────────────────────────────
    def _counts(self) -> dict:
        """台帳六狀態的計數（拿不到就空 dict）。"""
        if self.ledger is None:
            return {}
        try:
            rows = self.ledger.items()
        except Exception:               # noqa: BLE001 - 台帳壞了不擋這一步
            return {}
        counts = {key: 0 for key in _COUNT_KEYS}
        for item in rows or ():
            state = str((item or {}).get("state") or "")
            if state in counts:
                counts[state] += 1
        return counts

    def _diff_line(self, counts: dict, previous: dict) -> str:
        """台帳的**差量**（跟上一拍比）；第一次沒有基準就報現狀。"""
        if not counts:
            return ""
        if not previous:
            live = [f"{_LABELS[key]} {counts[key]}" for key in _COUNT_KEYS
                    if counts.get(key) and key in ("pending", "hanging", "locked")]
            return ("・台帳：" + "／".join(live)) if live else ""
        parts = []
        for key in _COUNT_KEYS:
            delta = int(counts.get(key) or 0) - int(previous.get(key) or 0)
            if delta > 0:
                parts.append(f"{_LABELS[key]} +{delta}")
        if not parts:
            return "・台帳：増減なし"
        return "・台帳：" + "／".join(parts)

    def _talked_since(self, since: float):
        if self.talked is None:
            return None
        try:
            return self.talked(self.entity, since)
        except Exception:               # noqa: BLE001 - 取不到就不寫這一行
            return None

    @staticmethod
    def _events_of(talked) -> list[dict]:
        """`talked` 的回傳正規化成 `[{label, count, snippet}]`（舊的 `(次數, 最後一句)` 也收）。"""
        if talked is None:
            return []
        if isinstance(talked, tuple) and len(talked) == 2 and isinstance(talked[0], int):
            count, snippet = int(talked[0]), str(talked[1] or "")
            return [{"label": "", "count": count, "snippet": snippet}] if count else []
        rows = []
        for item in talked or ():
            if not isinstance(item, dict):
                continue
            count = int(item.get("count") or 0)
            if count <= 0:
                continue
            rows.append({"label": str(item.get("label") or ""), "count": count,
                         "snippet": str(item.get("snippet") or "")})
        return rows

    def _notes_since(self, since: float):
        """她這段時間留下的筆記：`(件數, 最後那一段的標題)`；沒有筆記目錄回 None。"""
        if self.notes_root is None:
            return None
        folder = self.notes_root / self.entity
        try:
            files = [path for path in folder.glob("*.md")
                     if path.stat().st_mtime > since]
        except OSError:
            return None
        if not files:
            return (0, "")
        files.sort(key=lambda path: path.stat().st_mtime)
        last = ""
        try:
            for line in files[-1].read_text(encoding="utf-8").splitlines():
                if line.startswith("## "):
                    last = line[3:].strip()
        except OSError:
            last = ""
        return (len(files), last)

    @staticmethod
    def _scope_values(value) -> tuple:
        try:
            return tuple(value() if callable(value) else (value or ()))
        except Exception:                               # noqa: BLE001 - 不知道房間就拒絕投遞
            return ()

    @staticmethod
    def _activity_id(row: dict) -> str:
        raw = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _public_activity_lines(self, state: dict) -> list[str]:
        """其他角色的 public log；第一次只建基線，private 永遠不讀。"""
        if self.activity_log is None:
            return []
        seen = state.setdefault("activity_seen", {})
        lines = []
        for peer in self.peers:
            try:
                rows = self.activity_log.entries(peer, limit=20, visibility="public")
            except Exception:                           # noqa: BLE001 - 日誌壞了不擋心跳
                continue
            ids = [self._activity_id(row) for row in rows]
            previous = seen.get(peer)
            if previous is None:
                seen[peer] = ids[-64:]
                continue
            known = {str(item) for item in previous}
            pending = [(row, event_id) for row, event_id in zip(rows, ids)
                       if event_id not in known and str(row.get("text") or "").strip()]
            # 一次只取一件；其餘留在下一跳，避免截斷後卻已標記已讀。
            for row, event_id in pending[:1]:
                # 給時間行與角色前綴留空間；候選行若仍沒完整進最終文本，observe 會回捲已讀。
                budget = max(8, min(80, self.max_chars - 64))
                text = " ".join(str(row.get("text") or "").split())[:budget]
                lines.append(f"・{peer}の公開行動：{text}")
                seen[peer] = (list(previous) + [event_id])[-64:]
            if lines:
                break
        return lines

    def _event_lines(self, state: dict, *, public: bool) -> list[str]:
        if self.events is None:
            return []
        seen = [str(item) for item in (state.get("seen_events") or ())]
        rows = self.events.pending(
            self.entity, seen, principal=self.principal,
            guild_ids=self._scope_values(self.guild_ids),
            table_ids=self._scope_values(self.table_ids), public=public)
        rows = rows[:1]                              # 未取的事件下一跳再投，絕不静默丟棄
        if rows:
            state["seen_events"] = (seen + [str(row["id"]) for row in rows])[-1024:]
        # 時間行也占 max_chars；候選行先留下足夠空間，避免小視窗永遠投不完同一條。
        budget = max(8, min(80, self.max_chars - 40))
        return ["・" + " ".join(str(row["text"] or "").split())[:budget] for row in rows]

    # ── 對外 ─────────────────────────────────────────────────────────────────
    def observe(self, *, now: float | None = None, public: bool = False) -> str:
        """算一次「她看到的世界」，**不動上一歩的錨點**。

        心跳每跳呼叫它（owner 2026-11-03：觀察者層隨心跳刷新），所以她的獨白／下一步
        拿到的是「最近一次心跳時的世界」，而不是等到真的要做事才算。

        `public=True`（產出會進公開筆記的那條路）：**別人在房裡說的原話一律不給**，
        只留「哪個房間、幾回」——這是揭露面，不是記性問題。
        """
        moment = float(now if now is not None else self._clock())
        state = self._load()
        last = float(state.get("last_at") or 0.0)
        if self.shared is not None:
            self.shared.maybe_refresh(now=moment)      # 共享層：定時刷新
        hour, clock_text, _date_text = _now_parts(moment, self.tz)
        when = f"いま {clock_text}・{_part_of_day(hour)}"
        lines = []
        if last > 0 and last <= moment:
            lines.append(f"［前の一歩から {_gap_text(moment - last)}（{when}）］")
        else:
            lines.append(f"［{when}・この活動の最初の一歩］")
        diff = self._diff_line(self._counts(), state.get("counts") or {})
        if diff:
            lines.append(diff)
        seen_events_before = [str(item) for item in (state.get("seen_events") or ())]
        activity_before = {str(peer): list(ids or ())
                           for peer, ids in (state.get("activity_seen") or {}).items()}
        event_lines = self._event_lines(state, public=public)
        activity_lines = self._public_activity_lines(state)
        lines.extend(event_lines)
        lines.extend(activity_lines)
        if last > 0:
            talked = self._talked_since(last)
            if talked is not None:
                events = self._events_of(talked)
                total = sum(int(item["count"]) for item in events)
                if total:
                    from agent.brain.runtime.inner.rooms import human_label
                    grouped = [f"{human_label(item['label']) or '個別'} {item['count']} 回"
                               for item in events]
                    lines.append(f"・この間に声をかけられた：{total} 回（" + "／".join(grouped) + "）")
                    if not public:
                        for item in events:
                            snippet = str(item.get("snippet") or "").strip()[:40]
                            if snippet:
                                where = human_label(item["label"]) or "個別"
                                lines.append(f"　（{where}で最後に言われたこと：「{snippet}」）")
                else:
                    lines.append("・この間に誰も来なかった")
            notes = self._notes_since(last)
            if notes is not None:
                count, title = notes
                if count:
                    lines.append(f"・自分が残したノート：{count} 件"
                                 + (f"（最後：「{title[:40]}」）" if title else ""))
        text = "\n".join(lines)
        if self.shared is not None:
            shared_text = self.shared.block(now=moment, public=public)
            if shared_text:
                combined = f"{shared_text}\n{text}"
                # 一次性事件優先於常駐共享快照；否則共享區占滿時，同一事件會永遠無法投完。
                if (event_lines or activity_lines) and len(combined) > self.max_chars:
                    shared_text = ""
                if shared_text:
                    text = combined
        text = text[:self.max_chars]
        visible = set(text.splitlines())
        if any(line not in visible for line in event_lines):
            state["seen_events"] = seen_events_before
        if any(line not in visible for line in activity_lines):
            state["activity_seen"] = activity_before
        state.update({"observed_at": round(moment, 3), "text": text,
                      "counts": self._counts()})
        self._save(state)
        return text

    def block(self, *, now: float | None = None, public: bool = False) -> str:
        """給 prompt 的那一段：**現算一次**，然後推進「上一歩」錨點。

        錨點只在「她真的看到」時推進——所以隔了六小時再回來，看到的仍是「6 時間」，
        不會被心跳刷成「30 秒」。心跳那份（`observe()`）是給獨白用的快取，不影響這裡。

        `public=True`：產出會進公開筆記／對外訊息的那條路（見 `observe` 的說明）。
        """
        moment = float(now if now is not None else self._clock())
        text = self.observe(now=moment, public=public)
        state = self._load()
        state["last_at"] = round(moment, 3)
        state["counts"] = self._counts()
        self._save(state)
        return text[:self.max_chars]

    def latest(self, *, fresh_s: float | None = None) -> str:
        """最近一次觀察的文字（心跳刷新的那份）；太舊就回空字串。

        給獨白用：心跳每一跳刷新一次，所以獨白讀到的永遠是「剛剛看到的世界」。
        """
        state = self._load()
        text = str(state.get("text") or "")
        observed = float(state.get("observed_at") or 0.0)
        if not text or not observed:
            return ""
        limit = float(self.fresh_s if fresh_s is None else fresh_s)
        if self._clock() - observed > limit:
            return ""
        return text[:self.max_chars]

    def facts(self, *, fresh_s: float | None = None) -> str:
        """獨白用的那一小段：去掉共享層那行（共享層另有 `clock` 欄，不重複佔字數）。"""
        text = self.latest(fresh_s=fresh_s)
        lines = [line for line in text.splitlines()
                 if line.strip() and not line.startswith("［世界：")]
        return "\n".join(lines)[:400]


def talked_from_threads(threads, *, entity: str = ""):
    """「這段時間誰找過她」——**按房間聚合**（owner 2026-11-03：語義與分層記憶對齊）。

    回 `(entity, since) -> [{label, count, snippet}]`；`label` 用 `inner/rooms.label_of`，
    跟記憶層的 audience 是同一套表示（`pair(owner)`／`circle(3)`…）。

    只讀她自己的線（`thread.key.entity`），別人的線不進來；一條線都沒有 ⇒ 回 None
    （＝**不知道**，不寫「誰も来なかった」）。
    """
    if threads is None:
        return None

    def _talked(entity_id: str, since: float):
        from agent.brain.runtime.inner.rooms import label_of, room_of_thread
        who = str(entity or entity_id or "")
        try:
            rows = list(threads.threads())
        except Exception:                              # noqa: BLE001 - 取不到就不寫這一行
            return None
        events: dict[str, dict] = {}
        seen_any = False
        for thread in rows:
            if str(getattr(getattr(thread, "key", None), "entity", "")) != who:
                continue
            seen_any = True
            turns = [turn for turn in (getattr(thread, "transcript", ()) or ())
                     if float(getattr(turn, "at", 0.0) or 0.0) > float(since)
                     and str(getattr(turn, "role", "")) != "assistant"]
            if not turns:
                continue
            label = label_of(room_of_thread(thread, entity=who), entity=who)
            row = events.setdefault(label, {"label": label, "count": 0, "snippet": ""})
            row["count"] += len(turns)
            row["snippet"] = str(getattr(turns[-1], "text", "") or "")
        if not seen_any:
            return None                                # 一條線都沒有 ⇒ 不知道
        return list(events.values())

    return _talked


__all__ = ["SharedWorld", "WorldDelta", "talked_from_threads", "SHARED_KEYS"]
