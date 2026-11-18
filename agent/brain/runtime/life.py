"""她自己的活動：步驟執行器與預設事項（owner 2026-10-22 擴充）。

兩條規矩來自 owner：

  ① **她自己的活動要寫記憶**（不是只有對話才寫）。
  ② 行動要 **log 化**，並且分 **private / public**：
     · public：學習筆記、玩出來的結果 ⇒ 進記憶、**別人也讀得到**（其他 agent 回話時可以參考）
     · private：心情、失敗、碎念 ⇒ 只留在她的日誌裡，只有她自己能回溯

典型活動（owner 指定的兩個方向）：
  · `study`：自己查資料學習（讀 wiki／追一篇 paper／記一頁筆記）
  · `play`：自己去玩（跑一款遊戲、逛一圈、試一個新東西）
另外保留原有的 `trpg`／`work`／`ambient`，以及 `rest`（純私密：休息、發呆）。
"""

from __future__ import annotations

import inspect
import json
import os
import pathlib
import secrets
import threading
import time

from agent.brain.ports import FactSinkPort
from agent.brain.domain.activity import Activity
from agent.brain.domain.entity import EntityRegistry
from agent.brain.runtime.activity_log import PRIVATE, PUBLIC, ActivityLog
from agent.brain.runtime.fileperm import adopt_owner
# 活動庫（`Preset`／`PRESETS`／`INPUTS`／`DEFAULT_PRESET`）是純資料，在 `runtime/inner/presets.py`；這裡轉出，舊 import 照用
from agent.brain.runtime.inner.presets import DEFAULT_PRESET, INPUTS, PRESETS, Preset  # noqa: F401


def _in_window(hour: int, window: tuple[int, int] | None) -> bool:
    if window is None:
        return True
    start, end = int(window[0]) % 24, int(window[1]) % 24
    hour = int(hour) % 24
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end           # 跨午夜（例：22→2）


def presets_for(entity: str) -> tuple[Preset, ...]:
    """這個角色的活動庫（沒有專屬的就只有一個保守預設）。"""
    found = PRESETS.get(str(entity))
    library = tuple(found) if found else (
        Preset(DEFAULT_PRESET.key, DEFAULT_PRESET.title.format(entity=entity),
               DEFAULT_PRESET.kind, DEFAULT_PRESET.steps),)
    return library + _report_presets(entity, library)


def _report_presets(entity: str, library: tuple[Preset, ...]) -> tuple[Preset, ...]:
    """每日報：每個角色各一套（時段由 `BLUEAGENT_REPORT_HOUR` 定，`off` 可關）。

    已經有同名 key 的就不重複加（避免自訂庫撞名）。
    """
    from agent.brain.runtime.daily_report import report_hour, report_preset
    hour = report_hour()
    if hour is None or any(item.key == "daily-report" for item in library):
        return ()
    return (report_preset(entity, hour=hour),)


def preset_for(entity: str) -> Preset:
    """（相容用）活動庫裡的第一套。挑選走 `pick_preset`。"""
    return presets_for(entity)[0]


class Rotation:
    """活動輪換：**不連做同一套**、**今天做過最多次的排最後**。

    狀態落盤（`life-rotation.json`），重啟不會每次都從第一套開始；檔案壞掉就當空的
    （寧可少一點輪換，也不要因為一個 json 讓生活迴圈起不來）。
    """

    def __init__(self, path=None, *, keep: int = 24, daily_cap: int = 0,
                 min_repeat_h: float = 0.0, heard=None) -> None:
        self.path = pathlib.Path(path) if path is not None else None
        self.keep = max(1, int(keep))
        #: 同一套活動**一天最多排幾次**（0＝不限制）。2026-10-31 owner：她一天把同樣 5-7 套
        #  重跑約 55 輪（歷史只留 24 筆＝約 2 小時，等於「今天做過幾次」根本算不出來）。
        self.daily_cap = max(0, int(daily_cap))
        #: 兩次同一套之間至少隔幾小時（`daily_cap` 全滿時的退路；0＝不限制）
        self.min_repeat_h = max(0.0, float(min_repeat_h))
        #: 歷史保留窗（小時）：要涵蓋一整天，`keep` 只是下限
        self.keep_hours = 48.0
        self._lock = threading.RLock()
        self._data: dict[str, list[dict]] = self._load()
        #: 每套活動「上一次做到哪、是不是還開著」——跨活動延續用（owner 2026-10-27 第 4 條）
        self._closes: dict[str, dict[str, dict]] = self._load_closes()
        self._continuation = ""
        #: 防自旋（`runtime/inner/spin.py`）：`heard(entity) -> float`＝主人最後一次對她說話的時刻；
        #: `_novelty[entity][key]`＝這套最近幾次的產出、跟上次一樣幾次、冷卻到幾點（落盤）
        self.heard = heard
        self._novelty: dict[str, dict[str, dict]] = self._load_part("novelty")
        #: 上一次 `pick` 為什麼排不出來（觀測用）
        self.last_skip: dict[str, str] = {}

    # ── 檔案 ────────────────────────────────────────────────────────────────
    def _load(self) -> dict[str, list[dict]]:
        if self.path is None:
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if isinstance(raw, dict) and isinstance(raw.get("entities"), dict):
            raw = raw["entities"]
        out: dict[str, list[dict]] = {}
        if isinstance(raw, dict):
            for entity, rows in raw.items():
                if isinstance(rows, list):
                    out[str(entity)] = [row for row in rows if isinstance(row, dict)]
        return out

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 暫存檔名每個 process 唯一（固定 `.tmp` 會被兩個 writer 互相截斷）
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
            payload = {"entities": self._data, "closes": self._closes, "novelty": self._novelty}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            adopt_owner(tmp)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _load_closes(self) -> dict[str, dict[str, dict]]:
        return self._load_part("closes")

    def _load_part(self, name: str) -> dict[str, dict[str, dict]]:
        if self.path is None:
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        closes = raw.get(name) if isinstance(raw, dict) else None
        out: dict[str, dict[str, dict]] = {}
        if isinstance(closes, dict):
            for entity, rows in closes.items():
                if isinstance(rows, dict):
                    out[str(entity)] = {str(key): dict(value) for key, value in rows.items()
                                        if isinstance(value, dict)}
        return out

    # ── 跨活動延續（owner 2026-10-27 第 4 條）────────────────────────────────
    def note_close(self, entity: str, key: str, *, close: str, is_open: bool,
                   now: float | None = None) -> None:
        """一件活動收尾：記下最後一句，以及「是不是還有沒弄完的」。"""
        moment = float(now if now is not None else time.time())
        with self._lock:
            self._closes.setdefault(str(entity), {})[str(key)] = {
                "close": str(close or "")[:200], "open": bool(is_open),
                "at": round(moment, 3)}
            self._save()

    def last_close(self, entity: str, key: str, *, within_h: float = 24.0,
                   now: float | None = None) -> dict | None:
        moment = float(now if now is not None else time.time())
        with self._lock:
            row = (self._closes.get(str(entity)) or {}).get(str(key))
        if not row:
            return None
        if moment - float(row.get("at") or 0.0) > within_h * 3600:
            return None
        return dict(row)

    def note_output(self, entity: str, key: str, text: str, *, now: float | None = None) -> dict:
        """一件活動收尾的產出：跟同一套最近 3 次比，太像就冷卻這套（防自旋 ②）。回這套的新狀態。"""
        from agent.brain.runtime.inner import spin
        moment = float(now if now is not None else time.time())
        with self._lock:
            table = self._novelty.setdefault(str(entity), {})
            table[str(key)] = spin.note_output(table.get(str(key)) or {}, text, now=moment)
            self._save()
            return dict(table[str(key)])

    def take_continuation(self) -> str:
        """把「接著做」的那一句取走（一次）。"""
        text, self._continuation = self._continuation, ""
        return text

    # ── 挑選 ────────────────────────────────────────────────────────────────
    def history(self, entity: str) -> list[dict]:
        with self._lock:
            return list(self._data.get(str(entity)) or ())

    def pick(self, entity: str, presets, *, hour: int | None = None,
             now: float | None = None) -> "Preset | None":
        """挑下一套活動；全都在「今天做滿了／還沒休息夠」⇒ 回 None（今天到這裡為止）。"""
        library = tuple(presets or ())
        if not library:
            raise ValueError("empty preset library")
        clock = time.time() if now is None else float(now)
        oclock = int(time.localtime(clock).tm_hour) if hour is None else int(hour) % 24

        history = self.history(entity)
        used_today = [row for row in history
                      if _same_day(float(row.get("at") or 0), clock)]
        recent = {str(row.get("key") or "") for row in history[-max(1, len(library) - 1):]}
        counts: dict[str, int] = {}
        last_at: dict[str, float] = {}
        for row in used_today:
            key = str(row.get("key") or "")
            counts[key] = counts.get(key, 0) + 1
        for row in history:
            key = str(row.get("key") or "")
            last_at[key] = max(last_at.get(key, 0.0), float(row.get("at") or 0.0))

        from agent.brain.runtime.inner import spin
        picks = {name: list(rows) for name, rows in self._data.items()}
        kinds = {name: {item.key: spin.inputs_of(item) for item in presets_for(name)} for name in picks}
        kinds[str(entity)] = {item.key: spin.inputs_of(item) for item in library}
        novelty = self._novelty.get(str(entity)) or {}
        heard_at = 0.0
        if self.heard is not None:
            try:
                heard_at = float(self.heard(str(entity)) or 0.0)
            except Exception:                          # noqa: BLE001 - 問不到就當沒說過話
                heard_at = 0.0
        today_keys = [str(row.get("key") or "") for row in used_today]

        def unspun(item: Preset) -> bool:
            """防自旋三條：沒在冷卻、閉環的有新材料、閉環配額沒滿。"""
            if spin.cooling(novelty.get(item.key), now=clock):
                return False
            if not spin.is_closed(item):
                return True
            return (spin.has_fresh_material(str(entity), item, since=last_at.get(item.key, 0.0),
                                            picks=picks, kinds=kinds, heard_at=heard_at)
                    and spin.closed_quota_ok(today_keys, kinds[str(entity)]))

        def candidates(allow_recent: bool) -> list[Preset]:
            pool = [p for p in library if _in_window(oclock, p.hours)] or list(library)
            pool = [p for p in pool if unspun(p)]
            if not allow_recent:
                fresh = [p for p in pool if p.key not in recent]
                if fresh:
                    pool = fresh
            # 「每 N 天一次」的活動：還沒到期就不排（技能整理不需要每天做）
            # ⚠️ 過濾後空的就是空的：以前「空就整批放回去」會讓週期活動照樣天天跑（實測抓到）。
            pool = [p for p in pool
                    if int(getattr(p, "every_days", 0) or 0) <= 0
                    or clock - last_at.get(p.key, 0.0) >= int(p.every_days) * 86400]
            # 今天做過最少的優先；一樣多就挑最久沒做的
            return sorted(pool, key=lambda p: (counts.get(p.key, 0), last_at.get(p.key, 0.0),
                                               library.index(p)))

        def under_cap(pool: list[Preset]) -> list[Preset]:
            if self.daily_cap <= 0:
                return list(pool)
            return [p for p in pool if counts.get(p.key, 0) < self.daily_cap]

        def rested(pool: list[Preset], now_ts: float) -> list[Preset]:
            """距離上次做這套至少 `min_repeat_h` 小時（沒做過的一律算休息夠）。"""
            if self.min_repeat_h <= 0:
                return list(pool)
            gap = self.min_repeat_h * 3600.0
            return [p for p in pool if now_ts - last_at.get(p.key, 0.0) >= gap]

        # 每日報優先（`once_per_day`）：不受每日上限影響——那是「一天一次的收尾」，排不進也得排一次
        for preset in library:
            if not getattr(preset, "once_per_day", False):
                continue
            if counts.get(preset.key, 0) or not _in_window(oclock, preset.hours):
                continue
            self._continuation = ""
            self._remember(entity, preset.key, clock)
            return preset

        pool = candidates(allow_recent=False)
        # ① 先扣掉「今天已經做滿」的；② 再扣掉「剛做過、還沒休息夠」的
        ready = rested(under_cap(pool), clock)
        if not ready:
            # 退一步：允許挑「剛做完不久」的那幾套，但**不放寬每日上限**
            ready = rested(under_cap(candidates(allow_recent=True)), clock)
        if not ready:
            # 全部都做滿了／都還太近／閉環沒新材料 ⇒ **今天先到這裡**（交回 None 讓生活迴圈去休息）
            self.last_skip[str(entity)] = "nothing_fresh"
            return None
        chosen = ready[0]
        # 跨活動延續：優先「上一次還開著」的那套（時段內、24h 內）——不然她每天只是從菜單
        # 隨機點一道，讀起來沒有研究線（owner 2026-10-27 第 4 條）。
        # ⚠️ 只在「沒做滿、也休息夠」的裡面找：模型幾乎每件都說「還沒弄完」，以前掃整個 library ⇒
        #    「還開著」那套可以無視每日上限與休息間隔一直被挑（實測 100 分鐘內同一套 10 次）。
        #    「剛做過」（`recent`）只是換口味的軟規則，延續不受它限制——不然延續幾乎永遠輪不到。
        self._continuation = ""
        for preset in rested(under_cap(candidates(allow_recent=True)), clock):
            if not _in_window(oclock, preset.hours):
                continue
            last = self.last_close(entity, preset.key, now=clock)
            if last and last.get("open") and str(last.get("close") or "").strip():
                chosen = preset
                self._continuation = str(last.get("close"))
                break
        self._remember(entity, chosen.key, clock)
        return chosen

    def _remember(self, entity: str, key: str, clock: float) -> None:
        """記一筆「這一套在什麼時候排過」（輪換與每日上限都靠它）。"""
        with self._lock:
            rows = self._data.setdefault(str(entity), [])
            rows.append({"key": str(key), "at": round(clock, 3)})
            self._prune(rows, clock)
            self._save()

    def _prune(self, rows: list[dict], clock: float) -> None:
        """歷史只留 `keep_hours` 內（至少留 `keep` 筆）——不然「今天做過幾次」算不出來。"""
        horizon = clock - self.keep_hours * 3600.0
        fresh = [row for row in rows if float(row.get("at") or 0.0) >= horizon]
        if len(fresh) < self.keep:
            fresh = rows[-self.keep:]
        rows[:] = fresh


def _same_day(a: float, b: float) -> bool:
    if a <= 0:
        return False
    return time.localtime(a)[:3] == time.localtime(b)[:3]


def build_activity(entity: str = "mira", *, rotation: "Rotation | None" = None,
                  after: str = "", hour: int | None = None) -> "Activity | None":
    """排下一件事。`rotation` 給了就按輪換挑（不連做同一套），`after` 是上一次的 key。"""
    library = presets_for(entity)
    if rotation is not None:
        preset = rotation.pick(entity, library, hour=hour)
        if preset is None:
            return None                      # 今天做夠了：交回 None，讓生活迴圈去休息
    else:
        pool = [item for item in library if item.key != str(after or "")] or list(library)
        now_hour = int(time.localtime().tm_hour) if hour is None else int(hour) % 24
        fit = [item for item in pool if _in_window(now_hour, item.hours)]
        preset = (fit or pool)[0]
    activity = Activity.create(entity, preset.title, [text for text, _ in preset.steps],
                               kind=preset.kind,
                               visibilities=[visibility for _, visibility in preset.steps])
    activity.meta["preset"] = preset.key
    activity.meta["form"] = preset.form
    activity.meta["fiction"] = 1 if getattr(preset, "fiction", False) else 0
    activity.meta["brief"] = preset.brief
    if rotation is not None:
        # 上一次這套做到哪（只有「還開著」才有）——第一步會帶著它開場
        continuation = rotation.take_continuation()
        if continuation:
            activity.meta["continuation"] = continuation
    return activity


def preset_key_of(activity) -> str:
    """這件活動是用哪一套排出來的（給下一次輪換排除用）。"""
    if activity is None:
        return ""
    try:
        return str((activity.meta or {}).get("preset") or "")
    except AttributeError:
        return ""


__all__ = ["DEFAULT_PRESET", "PRESETS", "Preset", "Rotation", "build_activity",
           "make_step_fn", "preset_for", "preset_key_of", "presets_for"]


# 相容：步驟執行器搬到 `life_step`（life.py 只留活動庫／輪換／排程）
from agent.brain.runtime.life_step import NET_TOOLS, make_step_fn  # noqa: E402,F401

# 相容：一天結束的自我總結提示詞也搬到 `life_step`（2026-10-27 拆檔）
from agent.brain.runtime.life_step import REFLECT_PROMPT  # noqa: E402,F401
