"""內驅：把「能做而不做」換成「有帳沒結、有假設沒驗」（owner 2026-11-09）。

為什麼不是「時間到了就催」：tick 只是**驅動源**，不是需求。時間一到就催，模型最省力的
合規方式是做假動作（把已知的再查一次、寫一句「確認した」）——那正是 `inner/spin.py`
要壓掉的東西。所以這裡只認**具體的未結項**，並且用「有沒有 world diff」給每一次出手記帳：
沒 diff 就冷卻這個來源（2h→4h→8h），有 diff 才算真的動過。

三個來源（全部是既有資料，不新增存儲）：
  · `delivery`：主人要的交付（`runtime/inner/pending_delivery.py`）還沒交
  · `task`：任務台帳停在非完成狀態太久（`data/tasks/<entity>.json`）
  · `verify`：她的作品裡自己留下「未検証／要確認」的假設（`data/works/<entity>/*.md`）

狀態檔 `data/drive/<entity>.json`：`{pending, refs, resolved, stats, last_nudge_at, last_try_at, day, count_today}`。
純函式為主，檔案 I/O 只在這支（心臟不碰資料形狀）。

2026-11-09 評估後修正（第一版的帳對不上真資料）：
  · 交付讀的是 `PendingDeliveryStore`（`cleared_at`／`expired_at`＋TTL），不是自創的 `done_at`；
  · 任務的「未結」按台帳自己的狀態（pending／processing／hanging），`locked` 是故意等著的不算；
  · **等那一件做完才結帳**（`running` 回 False 之後），不是出手 90 秒就判空轉——真大腦一步至少 60 秒；
  · 冷卻按**那一條**（kind:ref），一條卡住不擋同類別的其他條；驗過的假設檔沒再改就不再撿；
  · 開不成（睡著／心情要休息／手上有事）也記一筆 `last_try_at`，15 分內不重試（不每個心跳都去問）。
"""

from __future__ import annotations

import json
import pathlib
import re
import time

from agent.brain.runtime.inner.pending_delivery import PendingDeliveryStore

#: 交付類：主人問過至少這麼久還沒交才算「欠著」
DELIVERY_MIN_S = 1800.0
#: 任務類：停在非完成狀態至少這麼久
TASK_MIN_S = 2700.0
#: 作品類：自己寫下「未驗證」的字眼
VERIFY_MARKS = re.compile(r"未検証|未確認|要確認|分からなかった")
#: 作品類只掃最近的幾份（不整庫掃）
VERIFY_SCAN = 40
#: 每種來源的基礎權重（交付最重：那是對人的承諾）
WEIGHTS = {"delivery": 1.0, "task": 0.75, "verify": 0.55}
#: 冷卻階梯（秒）：同一條第一次空轉 2h，之後 4h、8h（上限 8h）；假設類 ×3（驗一次很貴，也不急）
COOLDOWN_STEPS = (7200.0, 14400.0, 28800.0)
COOLDOWN_SCALE = {"verify": 3.0}
#: 任務台帳（`inner/task_ledger.py`）自己的「未結」三態；`locked` 是故意等著的（wait_until），不催
TASK_OPEN_STATES = frozenset({"pending", "processing", "hanging"})
#: 出手後至少等這麼久才結帳；那一件還在做就再等，最多等 SETTLE_MAX_S（卡死也要結）
SETTLE_MIN_S = 90.0
SETTLE_MAX_S = 3 * 3600.0
#: 開不成（睡著／想休息／手上有事）之後多久再試
RETRY_S = 900.0


def _slug(value, fallback: str = "unknown") -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return clean or fallback


def path_for(root, entity: str) -> pathlib.Path:
    """狀態檔放在 `<資料根>/drive/<entity>.json`（`root` 是資料根，跟 signals 用同一個）。"""
    return pathlib.Path(root) / "drive" / f"{_slug(entity)}.json"


def load(root, entity: str) -> dict:
    path = path_for(root, entity)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    body.setdefault("pending", None)
    body.setdefault("refs", {})          # "kind:ref" → {"until": 冷卻到, "spins": 空轉次數}
    body.setdefault("resolved", {})      # "verify:<檔名>" → 驗過的時刻（檔案之後沒改就不再撿）
    body.setdefault("last_try_at", 0.0)
    body.setdefault("stats", {"nudges": 0, "productive": 0, "spin": 0})
    body.setdefault("last_nudge_at", 0.0)
    body.setdefault("day", "")
    body.setdefault("count_today", 0)
    return body


def save(root, entity: str, state: dict) -> None:
    path = path_for(root, entity)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


# ── 三個來源 ────────────────────────────────────────────────────────────────
def _open_deliveries(base, entity: str, now: float) -> list[dict]:
    """還開著的欠帳——跟回話那邊同一個 store（`cleared_at`＝交了、`expired_at`＝過了 TTL）。"""
    store = PendingDeliveryStore(pathlib.Path(base) / "pending_delivery", clock=lambda: now)
    return store.items(_slug(entity))


def _deliveries(base, entity: str, now: float) -> list[dict]:
    rows = []
    for item in _open_deliveries(base, entity, now):
        asked = float(item.get("asked_at") or 0.0)
        age = now - asked
        if age < DELIVERY_MIN_S:
            continue
        what = str(item.get("what") or "").strip()
        if not what:
            continue
        rows.append({
            "kind": "delivery", "ref": str(item.get("id") or what[:16]), "age_s": round(age),
            "why": f"主人問過「{what[:40]}」，還沒交（{age / 3600:.1f} 小時前）",
            "title": "溜まっている交付を一つ片付ける",
            # 第一次實測（2026-11-09）：她在 home 目錄翻了一圈就放棄（材料其實在 works/ 或筆記裡）
            # ⇒ 步驟直接指名「去哪裡找」與「用哪個工具送」。
            "steps": [f"「{what[:40]}」に応えるため、{_slug(entity)} の書き出し（works/）かノートを一つ確かめる",
                      "材料があったら file_send で送る。送れないなら、何が足りないかを一行で書く"],
        })
    return rows


def _tasks(base, entity: str, now: float) -> list[dict]:
    path = pathlib.Path(base) / "tasks" / f"{_slug(entity)}.json"
    try:
        items = json.loads(path.read_text(encoding="utf-8")).get("items") or []
    except (OSError, ValueError, AttributeError):
        return []
    rows = []
    for item in items:
        if str(item.get("state") or "") not in TASK_OPEN_STATES:
            continue
        changed = float(item.get("updated_at") or item.get("created_at") or 0.0)
        age = now - changed
        if age < TASK_MIN_S:
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        cursor = item.get("cursor")
        total = item.get("steps")
        where = f"（{cursor}/{total} 步）" if cursor is not None and total else ""
        rows.append({
            "kind": "task", "ref": str(item.get("id") or title[:16]), "age_s": round(age),
            "why": f"「{title[:40]}」停在 {item.get('state')}{where}，{age / 3600:.1f} 小時沒動",
            "title": "止まっている自分の仕事を一段だけ進める",
            "steps": [f"「{title[:40]}」の続きを一段だけ進める（道具を一つだけ使う）",
                      "進んだことを一行で書く（進まなかったら、なぜ止まったか）"],
        })
    return rows


def _works(base, entity: str, now: float) -> list[dict]:
    folder = pathlib.Path(base) / "works" / _slug(entity)
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    rows = []
    for path in files[:VERIFY_SCAN]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = VERIFY_MARKS.search(text)
        if not match:
            continue
        start = max(0, match.start() - 30)
        snippet = re.sub(r"\s+", " ", text[start:match.start() + 40]).strip()
        rows.append({
            "kind": "verify", "ref": path.name, "age_s": round(now - path.stat().st_mtime),
            "mtime": path.stat().st_mtime,
            "why": f"《{path.stem[:36]}》裡留了「{match.group(0)}」：…{snippet[:50]}…",
            "title": "書きっぱなしの仮説を一つ確かめる",
            "steps": [f"《{path.stem[:36]}》の「{match.group(0)}」の箇所を、道具で一つ確かめる",
                      "確かめた結果を一行で書く（違っていたら、そう書く）"],
        })
    return rows


def signals(base, entity: str, *, now: float | None = None, limit: int = 6) -> list[dict]:
    moment = float(now if now is not None else time.time())
    rows: list[dict] = []
    for collect in (_deliveries, _tasks, _works):
        try:
            rows.extend(collect(base, entity, moment))
        except Exception:                                # noqa: BLE001 - 一個來源壞了不擋其他
            continue
    for row in rows:
        # 越久沒動越優先；交付類額外加權（那是對人的承諾）
        urgency = min(1.0, row["age_s"] / (6 * 3600.0))
        row["score"] = round(WEIGHTS.get(row["kind"], 0.5) + 0.5 * urgency, 3)
    rows.sort(key=lambda row: (-row["score"], row["age_s"]))
    return rows[:max(1, int(limit))]


def fingerprint(base, entity: str, *, now: float | None = None) -> str:
    """「世界有沒有變」的便宜指紋：任務狀態分佈 ＋ 作品數 ＋ 筆記數 ＋ 未交付數。

    只用來判「這次出手有沒有留下東西」，不是嚴謹的 state diff。
    `now` 必須跟著呼叫端的時鐘走（2026-11-15 WP6）：`_open_deliveries` 會按 `now`
    就地把逾期的未交付項標成 `expired_at` 並寫盤，若誤讀 `time.time()` 會在歷史時鐘下把未過期項提前清掉。
    """
    moment = float(now if now is not None else time.time())
    counts: dict[str, int] = {}
    try:
        items = json.loads((pathlib.Path(base) / "tasks" / f"{_slug(entity)}.json")
                           .read_text(encoding="utf-8")).get("items") or []
        for item in items:
            key = str(item.get("state") or "?")
            counts[key] = counts.get(key, 0) + 1
    except (OSError, ValueError, AttributeError):
        pass
    def _count(folder: str) -> int:
        path = pathlib.Path(base) / folder / _slug(entity)
        return len(list(path.glob("*.md"))) if path.is_dir() else 0

    works, notes = _count("works"), _count("notes")
    try:
        deliveries = len(_open_deliveries(base, entity, moment))
    except Exception:                                    # noqa: BLE001
        deliveries = 0
    return json.dumps([sorted(counts.items()), works, notes, deliveries], ensure_ascii=False)


# ── 出手與結算 ──────────────────────────────────────────────────────────────
def _key(row: dict) -> str:
    return f"{row.get('kind')}:{row.get('ref')}"


def cooled(state: dict, row: dict, now: float) -> bool:
    """這一條在冷卻中，或（假設類）驗過之後檔案沒再改。"""
    entry = (state.get("refs") or {}).get(_key(row)) or {}
    if float(entry.get("until") or 0.0) > now:
        return True
    if row.get("kind") == "verify":
        resolved = float((state.get("resolved") or {}).get(_key(row)) or 0.0)
        if resolved and float(row.get("mtime") or 0.0) <= resolved:
            return True
    return False


def pick(root, entity: str, rows: list[dict], *, now: float | None = None,
         gap_s: float = 2700.0, daily_cap: int = 4) -> dict | None:
    moment = float(now if now is not None else time.time())
    state = load(root, entity)
    day = time.strftime("%Y%m%d", time.localtime(moment))
    if state.get("day") != day:
        state["day"] = day
        state["count_today"] = 0
        save(root, entity, state)
    if state.get("pending"):
        return None                                      # 上一筆還沒結帳
    if int(state.get("count_today") or 0) >= int(daily_cap):
        return None
    if moment - float(state.get("last_nudge_at") or 0.0) < float(gap_s):
        return None
    if moment - float(state.get("last_try_at") or 0.0) < RETRY_S:
        return None
    for row in rows:
        if not cooled(state, row, moment):
            return row
    return None


def note_try(root, entity: str, *, now: float | None = None) -> None:
    """開不成（睡著／想休息／手上有事）⇒ 記一筆，`RETRY_S` 內不再去問。"""
    state = load(root, entity)
    state["last_try_at"] = float(now if now is not None else time.time())
    save(root, entity, state)


def note_nudge(root, entity: str, row: dict, fingerprint_now: str, *,
               now: float | None = None, activity_id: str = "") -> None:
    moment = float(now if now is not None else time.time())
    state = load(root, entity)
    state["pending"] = {"kind": row["kind"], "ref": row["ref"], "at": moment,
                        "fingerprint": fingerprint_now, "activity_id": str(activity_id or ""),
                        "mtime": row.get("mtime")}
    state["last_nudge_at"] = moment
    state["count_today"] = int(state.get("count_today") or 0) + 1
    state["stats"]["nudges"] = int(state["stats"].get("nudges") or 0) + 1
    save(root, entity, state)


def settle(root, entity: str, fingerprint_now: str, *, now: float | None = None,
           min_age: float = SETTLE_MIN_S, running=None) -> dict | None:
    """上一次出手結帳：世界有變 ⇒ productive；沒變 ⇒ 空轉，冷卻**那一條**。

    `running(entity, activity_id) -> bool`：那一件還在做 ⇒ 先不結（最多等 `SETTLE_MAX_S`）。
    """
    moment = float(now if now is not None else time.time())
    state = load(root, entity)
    pending = state.get("pending") or {}
    if not pending:
        return None
    age = moment - float(pending.get("at") or 0.0)
    if age < float(min_age):
        return None
    if running is not None and age < SETTLE_MAX_S:
        try:
            if running(entity, str(pending.get("activity_id") or "")):
                return None
        except Exception:                                # noqa: BLE001 - 問不到就照時間結
            pass
    kind = str(pending.get("kind") or "")
    key = _key(pending)
    changed = fingerprint_now != str(pending.get("fingerprint") or "")
    stats = state["stats"]
    refs = state.setdefault("refs", {})
    if changed:
        stats["productive"] = int(stats.get("productive") or 0) + 1
        refs.pop(key, None)
        if kind == "verify":
            state.setdefault("resolved", {})[key] = moment
    else:
        stats["spin"] = int(stats.get("spin") or 0) + 1
        entry = refs.setdefault(key, {"until": 0.0, "spins": 0})
        entry["spins"] = int(entry.get("spins") or 0) + 1
        step = COOLDOWN_STEPS[min(len(COOLDOWN_STEPS) - 1, entry["spins"] - 1)]
        entry["until"] = moment + step * COOLDOWN_SCALE.get(kind, 1.0)
    state["pending"] = None
    save(root, entity, state)
    return {"kind": kind, "changed": changed, "ref": pending.get("ref")}


def attempt(entity: str, base, now: float, *, on_work, budget=None, log=None,
            gap_s: float = 2700.0, daily_cap: int = 4, running=None,
            hours: tuple[int, int] | None = None) -> bool:
    """心跳那一支：先結上一筆帳，再看有沒有值得出手的未結項。回 True＝真的開了一件。

    `on_work(entity, row)` 回活動 id（字串）或 True＝開了；回空／False＝開不成（睡著、想休息、
    手上有事——這些由呼叫端照生物鐘與 `willing` 判），之後 `RETRY_S` 內不再問。
    `hours` 只在沒有生物鐘的場合用（測試／最小組合）：不在這個區間就不動手。
    """
    moment = float(now)
    if base is None or on_work is None:
        return False
    if hours is not None:
        low, high = hours
        if not (low <= time.localtime(moment).tm_hour < high):
            return False
    say = log or (lambda _message: None)
    try:
        current = fingerprint(base, entity, now=moment)
        settled = settle(base, entity, current, now=moment, running=running)
        if settled:
            say(f"drive[{entity}]: 結帳 {settled['kind']} → "
                f"{'有進展' if settled['changed'] else '空轉（冷卻這一條）'}")
        if budget is not None and hasattr(budget, "allows") and not budget.allows(entity, 1.0):
            return False
        rows = signals(base, entity, now=moment)
        row = pick(base, entity, rows, now=moment, gap_s=gap_s, daily_cap=daily_cap)
        if row is None:
            return False
        started = on_work(entity, row)
        if not started:
            note_try(base, entity, now=moment)
            return False
        note_nudge(base, entity, row, current, now=moment,
                   activity_id=started if isinstance(started, str) else "")
        say(f"drive[{entity}]: 內驅 ⇒ {row['kind']}：{row['why'][:60]}")
        return True
    except Exception as exc:                             # noqa: BLE001 - 內驅壞了不能擋心跳
        say(f"drive[{entity}]: 略過（{type(exc).__name__}: {exc}）")
        return False


__all__ = ["attempt", "cooled", "fingerprint", "load", "note_nudge", "note_try", "pick", "save",
           "settle", "signals"]
