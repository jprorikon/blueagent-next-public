"""體檢器（哨兵）：一層一項，逐項先說「它在哪找證據」。

為什麼要有它（2026-10-25，判據借自 `cyanwell/dsh-agent-soul` 的 ECHO 哨兵；代碼是自己的）：

    他們的實測坑，三條都不是「漏了一個 bug」，是**體檢器本身的形狀錯了**：
      ① 某一層**完全沒有檢查項** —— 狀態檔停更一個月，體檢裡沒有一項會報；
      ② 只報數量不報質量 —— 「索引層」只報摘要數，159 條沒有門牌照樣 ✓；
      ③ 在錯的地方找痕跡 —— 去一個根本沒這種值的欄位裡找證據，於是「零介入」永遠成立。

    所以這裡的規矩寫死在代碼裡，不靠人記得：
      · **每層至少一項檢查**；某層 0 項 ⇒ `error`（結構洞，不是「沒事」）；
      · 讀不到／沒資料 ⇒ `pending`，**不准**顯示成 `ok`（「看不到」≠「沒有」）；
      · 每一項都要宣告 `evidence`（在哪找證據）；宣告不出來的那項自己記 `pending`；
      · 檢查自己拋錯 ⇒ `pending` ＋ 例外型別（哨兵壞掉也要看得見）。

用法：
    python3 agent/tools/doctor.py --data data --entity mira
    python3 agent/tools/doctor.py --data data --entity mira --json

退出碼：有任何 `error` ⇒ 1；只有 `warn`/`pending` ⇒ 0（沒壞，但沒看全）。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import time

OK, WARN, ERROR, PENDING = "ok", "warn", "error", "pending"
_RANK = {ERROR: 3, WARN: 2, PENDING: 1, OK: 0}

#: 八個面。判據：每一面都必須至少有一項檢查（`_structure()` 會抓）。
LAYERS = {
    "L1-self": "自我＋熱記憶（人格 core／user.md）",
    "L2-workbench": "工作台（承接段＝壓縮產物）",
    "L3-raw": "一手層（原文，只追加）",
    "L4-index": "索引（窗口摘要／事實）＋門牌",
    "L5-retrieval": "檢索門（search 吐門牌／open 吃門牌）",
    "V1-homeostasis": "穩態（情緒／預算／心跳）",
    "V2-observability": "觀測面（告警狀態）",
    "X-delivery": "對外（交付未結／帳號）",
}

#: 體檢時每份月檔最多讀尾端這麼多 bytes（抽樣用，不做全量解析）
_TAIL_BYTES = 256 * 1024

#: 這台機器上讀不到就當 pending 的資料夾（容器寫的 root 檔）；
#: **不准**因為讀不到就當 ok —— 那正是「看不到 ≠ 沒有」。
_CHECKS: list = []


def check(layer: str, name: str, evidence: str):
    """把一個檢查函式登記進來。`evidence` 是「它在哪找證據」，寫不出來就別登記。"""
    def wrap(fn):
        _CHECKS.append({"layer": layer, "name": name, "evidence": evidence, "run": fn})
        return fn
    return wrap


class Ctx:
    """體檢上下文：只給**唯讀**路徑，體檢器不改任何資料。"""

    def __init__(self, data_dir, entity: str, *, now: float | None = None) -> None:
        self.data = pathlib.Path(data_dir)
        self.entity = str(entity or "")
        self.now = float(now if now is not None else time.time())

    # 路徑都從 data 推導，避免每個檢查各自拼字串
    @property
    def persona(self) -> pathlib.Path:
        return self.data / "persona" / f"{self.entity}.core.json"

    @property
    def users_dir(self) -> pathlib.Path:
        return self.data / "users"

    @property
    def db(self) -> pathlib.Path:
        return self.data / "memory" / "facts.db"

    @property
    def raw(self) -> pathlib.Path:
        return self.data / "raw"

    @property
    def raw_entity(self) -> pathlib.Path:
        return self.raw / self.entity

    @property
    def threads(self) -> pathlib.Path:
        return self.data / "threads.json"

    @property
    def affect(self) -> pathlib.Path:
        return self.data / "affect" / self.entity

    @property
    def budget(self) -> pathlib.Path:
        return self.data / "budget" / f"{self.entity}.json"

    @property
    def alerts(self) -> pathlib.Path:
        return self.data / "alerts.json"

    @property
    def outbox(self) -> pathlib.Path:
        return self.data / "outbox" / "index.jsonl"

    @property
    def accounts(self) -> pathlib.Path:
        return self.data / "accounts.json"


def _age_days(stamp: float, now: float) -> float:
    return max(0.0, (now - float(stamp or 0)) / 86400.0)


def _raw_rows(ctx: Ctx, limit: int = 200) -> list[dict]:
    """最近 `limit` 行原文：**從檔案尾端讀固定大小的尾巴**，不把整個月檔吃進記憶體。

    ★ 判據（借自他們的坑 3）：凡是「整份檔案讀進來再處理」的地方，先問一句「它可能有多大」。
      月檔會長到幾十 MB，體檢器不該為了抽 200 行把整份解壓成字串。
    """
    rows: list[dict] = []
    for path in sorted(ctx.raw_entity.glob("*.jsonl"), reverse=True):
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > _TAIL_BYTES:
                    handle.seek(size - _TAIL_BYTES)
                    handle.readline()          # 丢掉被切半的那一行
                blob = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in reversed(blob.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
            if len(rows) >= limit:
                return rows
    return rows


# ── L1 自我＋熱記憶 ─────────────────────────────────────────────────────────
@check("L1-self", "persona-core", "data/persona/<entity>.core.json")
def _persona_core(ctx: Ctx):
    path = ctx.persona
    if not path.exists():
        return PENDING, f"沒有 {path.name}（人格 core 未建，不影響跑，但人格只有 fallback）"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except PermissionError as exc:
        return PENDING, f"讀不到（{type(exc).__name__}）"
    except ValueError as exc:
        return ERROR, f"JSON 壞了：{exc}"
    traits = None
    if isinstance(body, dict):
        # 實際形狀（2026-10-25 核對）：`trait_dimensions` ＋ `disposition_rules` ＋ `portrait`；
        # 舊版檢查只看 `traits` ⇒ 對真檔永遠 warn（**假警報比漏報更壞**，會訓練人忽略體檢）。
        traits = body.get("trait_dimensions") or body.get("traits")
    if not traits:
        return WARN, "core 沒有 trait_dimensions／traits（空的人格等於沒有判據）"
    rules = body.get("disposition_rules") if isinstance(body, dict) else None
    return OK, f"{len(traits)} 條 traits，{len(rules or [])} 條 if-then"


@check("L1-self", "userfile", "data/users/<principal>.md")
def _userfile(ctx: Ctx):
    folder = ctx.users_dir
    if not folder.is_dir():
        return PENDING, "沒有 data/users（還沒有人寫過身分檔）"
    files = list(folder.glob("*.md"))
    if not files:
        return PENDING, "users/ 是空的"
    try:
        sizes = [path.stat().st_size for path in files]
    except OSError as exc:
        return PENDING, f"讀不到（{type(exc).__name__}）"
    return OK, f"{len(files)} 份，最大 {max(sizes)} B"


# ── L2 工作台（承接段＝壓縮產物，必須帶門牌）────────────────────────────────
@check("L2-workbench", "carry-pointer", "data/threads.json 每條線的 carry_summary")
def _carry_pointer(ctx: Ctx):
    from agent.brain.runtime.memory import pointer as pointers
    try:
        body = json.loads(ctx.threads.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PENDING, "沒有 threads.json（還沒開過線）"
    except PermissionError as exc:
        return PENDING, f"讀不到（{type(exc).__name__}）"
    except ValueError as exc:
        return ERROR, f"threads.json 壞了：{exc}"
    threads = body.get("threads") if isinstance(body, dict) else None
    if not isinstance(threads, list) or not threads:
        return PENDING, "沒有線"
    carries = [str(item.get("carry_summary") or "") for item in threads
               if isinstance(item, dict)]
    nonempty = [text for text in carries if text.strip()]
    if not nonempty:
        return PENDING, f"{len(threads)} 條線都還沒換過段（承接段為空＝還沒發生過壓縮）"
    with_ptr = [text for text in nonempty if pointers.looks_like(text)]
    if not with_ptr:
        return ERROR, f"{len(nonempty)} 條承接段**一條都沒有門牌**（壓縮產物指不回原文）"
    if len(with_ptr) < len(nonempty):
        return WARN, f"{len(with_ptr)}/{len(nonempty)} 條承接段帶門牌（其餘是舊產物）"
    return OK, f"{len(nonempty)} 條全帶門牌"


# ── L3 一手層 ───────────────────────────────────────────────────────────────
@check("L3-raw", "raw-presence", "data/raw/<entity>/*.jsonl")
def _raw_presence(ctx: Ctx):
    folder = ctx.raw_entity
    if not folder.is_dir():
        return PENDING, f"沒有 {folder}（這個角色還沒落過原文）"
    files = sorted(folder.glob("*.jsonl"))
    if not files:
        return PENDING, "原文目錄是空的"
    rows = _raw_rows(ctx, limit=1)
    if not rows:
        return PENDING, "有檔案但讀不到任何一行（權限或空檔）"
    age = _age_days(float(rows[0].get("at") or 0), ctx.now)
    detail = f"{len(files)} 個月份檔，最新一行 {age:.1f} 天前"
    return (WARN if age > 30 else OK), detail


@check("L3-raw", "raw-turn-ids", "原文每一行的 turn 欄位（門牌的原料）")
def _raw_turn_ids(ctx: Ctx):
    rows = _raw_rows(ctx, limit=200)
    if not rows:
        return PENDING, "沒有原文可抽樣"
    missing = sum(1 for row in rows if not str(row.get("turn") or "").strip())
    if missing:
        return ERROR, f"抽樣 {len(rows)} 行，{missing} 行沒有 turn id（門牌無原料）"
    return OK, f"抽樣 {len(rows)} 行都有 turn id"


# ── L4 索引＋門牌 ───────────────────────────────────────────────────────────
@check("L4-index", "window-rows", "SQLite facts.db → window_summaries")
def _window_rows(ctx: Ctx):
    from agent.brain.runtime.memory.window_store import WindowStore
    if not ctx.db.exists():
        return PENDING, "沒有 facts.db（還沒提煉過）"
    stats = WindowStore(ctx.db).stats(ctx.entity)
    if not stats["total"]:
        return PENDING, "沒有窗口摘要（還沒跑過提煉）"
    return OK, f"{stats['total']} 條窗口摘要"


@check("L4-index", "window-pointers", "window_summaries.ptr／turn_ids 的覆蓋率")
def _window_pointers(ctx: Ctx):
    from agent.brain.runtime.memory.window_store import WindowStore
    if not ctx.db.exists():
        return PENDING, "沒有 facts.db"
    stats = WindowStore(ctx.db).stats(ctx.entity)
    total = int(stats["total"] or 0)
    if not total:
        return PENDING, "沒有窗口摘要可比對"
    with_ptr = int(stats["with_ptr"] or 0)
    if not with_ptr:
        return ERROR, f"{total} 條窗口摘要**一條都沒有門牌**（只剩轉述，回不去原文）"
    if with_ptr < total:
        return WARN, f"門牌覆蓋 {with_ptr}/{total}（舊產物待回填：tools/backfill_window_ptrs.py）"
    return OK, f"門牌覆蓋 {with_ptr}/{total}"


@check("L4-index", "facts-present", "SQLite facts.db → facts")
def _facts_present(ctx: Ctx):
    if not ctx.db.exists():
        return PENDING, "沒有 facts.db"
    try:
        db = sqlite3.connect(str(ctx.db))
        try:
            row = db.execute("SELECT COUNT(*) FROM facts WHERE entity = ? OR entity = ''",
                             (ctx.entity,)).fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return PENDING, f"查不到（{type(exc).__name__}）"
    count = int(row[0] if row else 0)
    if not count:
        return PENDING, "facts 是空的（寫入側還沒落過事實）"
    return OK, f"{count} 條事實"


# ── L5 檢索門 ───────────────────────────────────────────────────────────────
@check("L5-retrieval", "pointer-roundtrip", "抽樣 3 條帶門牌的窗口 → resolve 回 data/raw")
def _pointer_roundtrip(ctx: Ctx):
    """★ 這一項是門牌的**唯一硬判據**：門牌能不能真的打開原文。

    抽樣而不是全量：全量掃原文會慢，而「有一條打不開」就足以說鏈條斷了。
    """
    from agent.brain.runtime.memory import pointer as pointers
    from agent.brain.runtime.memory.window_store import WindowStore
    if not ctx.db.exists():
        return PENDING, "沒有 facts.db"
    if not _has_ptr_column(ctx):
        return PENDING, "舊庫還沒補 ptr 欄位（先跑一次提煉或 tools/backfill_window_ptrs.py）"
    ptrs = [str(row["ptr"] or "").strip()
            for row in WindowStore(ctx.db).recent(ctx.entity, limit=50)]
    ptrs = [item for item in ptrs if item]
    if not ptrs:
        return PENDING, "沒有帶門牌的窗口可抽樣"
    broken = []
    for item in ptrs[:3]:
        found = pointers.resolve(item, ctx.raw)
        if not found.get("ok"):
            broken.append(f"{item} → {found.get('reason')}")
    if broken:
        return ERROR, "門牌斷鏈：" + "；".join(broken)
    return OK, f"抽樣 {min(3, len(ptrs))} 條都能回到原文"


@check("L5-retrieval", "raw-log-reachable", "data/raw 可讀（open 的必要條件）")
def _raw_reachable(ctx: Ctx):
    folder = ctx.raw_entity
    if not folder.is_dir():
        return PENDING, "沒有原文目錄"
    files = sorted(folder.glob("*.jsonl"))
    if not files:
        return PENDING, "沒有月份檔"
    try:
        with files[0].open("r", encoding="utf-8", errors="replace") as handle:
            handle.readline()
    except OSError as exc:
        return PENDING, f"讀不到（{type(exc).__name__}）"
    return OK, "原文可讀"


# ── V1 穩態 ─────────────────────────────────────────────────────────────────
@check("V1-homeostasis", "affect-fresh", "data/affect/<entity>/*/state.json 的 mtime")
def _affect_fresh(ctx: Ctx):
    folder = ctx.affect
    if not folder.is_dir():
        return PENDING, "沒有情緒狀態（AEM 未跑過）"
    stamps = []
    for path in folder.glob("*/state.json"):
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            continue
    if not stamps:
        return PENDING, "有目錄但讀不到 state.json（權限或未寫過）——看不到不等於沒有"
    age = _age_days(max(stamps), ctx.now)
    return (WARN if age > 1 else OK), f"最後寫入 {age * 24:.1f} 小時前"


@check("V1-homeostasis", "budget-state", "data/budget/<entity>.json")
def _budget_state(ctx: Ctx):
    try:
        body = json.loads(ctx.budget.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PENDING, "沒有預算檔（心跳未跑過）"
    except PermissionError as exc:
        return PENDING, f"讀不到（{type(exc).__name__}）"
    except ValueError as exc:
        return ERROR, f"預算檔壞了：{exc}"
    return OK, f"{len(body) if isinstance(body, dict) else 0} 個欄位"


# ── V2 觀測面 ───────────────────────────────────────────────────────────────
@check("V2-observability", "alerts-state", "data/alerts.json（告警冷卻狀態）")
def _alerts_state(ctx: Ctx):
    try:
        body = json.loads(ctx.alerts.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PENDING, "沒有 alerts.json（還沒報過警，也可能從沒標定過）"
    except PermissionError as exc:
        return PENDING, f"讀不到（{type(exc).__name__}）"
    except ValueError as exc:
        return ERROR, f"alerts.json 壞了：{exc}"
    saved = float((body or {}).get("saved_at") or 0)
    if not saved:
        return WARN, "有檔案但沒有 saved_at（誰在寫它？）"
    return OK, f"最後一次報警 {_age_days(saved, ctx.now):.1f} 天前"


# ── X 對外 ──────────────────────────────────────────────────────────────────
@check("X-delivery", "outbox", "data/outbox/index.jsonl")
def _outbox(ctx: Ctx):
    try:
        lines = [line for line in ctx.outbox.read_text(encoding="utf-8").splitlines() if line.strip()]
    except FileNotFoundError:
        return PENDING, "沒有 outbox 索引（沒有斷線補拿紀錄）"
    except PermissionError as exc:
        return PENDING, f"讀不到（{type(exc).__name__}）"
    return OK, f"{len(lines)} 條交付紀錄"


@check("X-delivery", "accounts", "data/accounts.json")
def _accounts(ctx: Ctx):
    try:
        body = json.loads(ctx.accounts.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PENDING, "沒有 accounts.json（還沒設帳號）"
    except PermissionError as exc:
        return PENDING, f"讀不到（{type(exc).__name__}）"
    except ValueError as exc:
        return ERROR, f"accounts.json 壞了：{exc}"
    # 實際形狀：`{"version": 1, "accounts": {acct_id: {...}}}`
    rows = body.get("accounts") if isinstance(body, dict) else None
    rows = rows if isinstance(rows, dict) else {}
    master = [key for key, value in rows.items()
              if str((value or {}).get("role") or "") == "master"
              if isinstance(value, dict)]
    return (OK if master else WARN), f"{len(rows)} 個帳號，master {len(master)}"


@check("X-delivery", "accounts-vs-guests", "data/accounts.json + data/guests.json")
def _accounts_cover_approved_guests(ctx: Ctx):
    """每個「核可過的 guest」都要有帳號，否則重建容器時它會被漏掉（owner 2026-10-31）。

    機制：啟動的 `AuthService._migrate_legacy` 只從 `guests.json` 的 approved 名單重建帳號。
    以前 App 建的 guest **只**寫 `accounts.json`，所以那個檔一壞／被當空庫，帳號就沒了。
    現在簽發時會同時記進 `guests.json`（`GuestStore.remember_account`）；這項檢查就是在看
    「有沒有漏記」——**approved 名單裡有人卻沒有對應帳號就是漏記**。
    """
    try:
        accounts_raw = json.loads(ctx.accounts.read_text(encoding="utf-8"))
        guests_raw = json.loads((ctx.data / "guests.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PENDING, "還沒有 accounts.json／guests.json（還沒設帳號）"
    except PermissionError as exc:
        return PENDING, f"讀不到（{type(exc).__name__}）"
    except ValueError as exc:
        return ERROR, f"JSON 壞了：{exc}"
    rows = (accounts_raw.get("accounts") if isinstance(accounts_raw, dict) else None) or {}
    requests = (guests_raw.get("requests") if isinstance(guests_raw, dict) else None) or []
    approved = {str((item or {}).get("principal_id") or "") for item in requests
                if str((item or {}).get("status") or "") == "approved"}
    approved.discard("")
    present = {str((value or {}).get("principal") or "") for value in rows.values()
               if isinstance(value, dict)}
    missing = sorted(approved - present)
    if missing:
        return WARN, (f"{len(missing)} 個核可過的 guest 沒有帳號：{'、'.join(missing[:5])}"
                      f"（重建容器時會消失）")
    # 反方向：只活在 accounts.json 的 guest（例如 `/api/accounts/import` 匯入的——匯出檔只有雜湊，
    # 沒有明文可以寫進 guests.json）⇒ accounts.json 壞掉時就沒了，要靠備份（2026-10-31 複檢）
    guest_accounts = {str((value or {}).get("principal") or "") for value in rows.values()
                      if isinstance(value, dict) and str((value or {}).get("role") or "") == "guest"
                      and not (value or {}).get("revoked_at")}
    guest_accounts.discard("")
    only_accounts = sorted(guest_accounts - approved)
    if only_accounts:
        return WARN, (f"{len(only_accounts)} 個 guest 只在 accounts.json：{'、'.join(only_accounts[:5])}"
                      f"（多半是匯入的；那個檔壞掉時要靠備份還原）")
    return OK, f"{len(approved)} 個核可 guest 都有帳號"


# ── 結構洞（體檢器檢查自己）─────────────────────────────────────────────────
def _structure() -> list[dict]:
    """★ 判據③的本體：**某一層沒有任何檢查項，本身就是故障**。

    這一段刻意放在體檢器裡而不是文件裡——他們的實測就是「寫在文件上，然後没人看」。
    這些項目掛在 `S0-structure` 這一個偽層上（`subject` 指明它講的是哪一層），
    這樣輸出不會跟各層自己的項目交錯重複。
    """
    out = []
    for layer, label in LAYERS.items():
        count = sum(1 for item in _CHECKS if item["layer"] == layer)
        out.append({"layer": "S0-structure", "check": "has-checks", "evidence": "體檢器註冊表",
                    "subject": layer,
                    "status": OK if count else ERROR,
                    "detail": f"{layer}：{count} 項檢查" if count
                    else f"{layer} **這一層沒有任何檢查項**（{label}）"})
    missing = [item["name"] for item in _CHECKS if not str(item["evidence"] or "").strip()]
    out.append({"layer": "S0-structure", "check": "evidence-declared", "evidence": "每項的宣告",
                "status": ERROR if missing else OK,
                "detail": ("沒宣告證據位置的檢查：" + "、".join(missing)) if missing
                else f"{len(_CHECKS)} 項都宣告了證據位置"})
    return out


def run(data_dir, entity: str, *, now: float | None = None,
        only: str | None = None) -> dict:
    """跑一輪體檢。回 `{ok, worst, counts, items, layers}`；`only` 可只看某一層。"""
    ctx = Ctx(data_dir, entity, now=now)
    items: list[dict] = []
    for item in _CHECKS:
        if only and item["layer"] != only:
            continue
        try:
            status, detail = item["run"](ctx)
        except PermissionError as exc:
            status, detail = PENDING, f"讀不到（{type(exc).__name__}）"
        except Exception as exc:                       # noqa: BLE001 - 哨兵自己壞掉也要報
            status, detail = PENDING, f"檢查自己拋錯：{type(exc).__name__}: {exc}"
        items.append({"layer": item["layer"], "check": item["name"],
                      "evidence": item["evidence"], "status": status, "detail": detail})
    if not only:
        items = _structure() + items
    counts = {key: 0 for key in (OK, WARN, ERROR, PENDING)}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    worst = max((item["status"] for item in items), key=lambda key: _RANK.get(key, 0),
                default=OK)
    return {"ok": worst != ERROR, "worst": worst, "counts": counts,
            "entity": ctx.entity, "at": ctx.now, "items": items,
            "layers": len({item["layer"] for item in items if item["layer"] in LAYERS})}


def _has_ptr_column(ctx: Ctx) -> bool:
    try:
        db = sqlite3.connect(str(ctx.db))
        try:
            cols = {str(row[1]) for row in db.execute("PRAGMA table_info(window_summaries)")}
        finally:
            db.close()
    except sqlite3.Error:
        return False
    return "ptr" in cols


__all__ = ["Ctx", "ERROR", "LAYERS", "OK", "PENDING", "WARN", "check", "run"]
