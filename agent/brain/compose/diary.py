"""她自己的日記與工作成果（master 端「+」→ 日記；owner 2026-10-31）。

三個來源，都是生活迴圈與每日反思寫的檔，這裡只讀：

- `data/notes/<entity>/<日期>.md`：每一步的筆記（`runtime/life_step._write_note`），段落標頭
  `## HH:MM 「活動」／步驟[〔private〕][〔作品：檔名〕]`
- `data/notes/<entity>/reflection-<日期>.md`：每天收尾的三行日記（`runtime/reflection.py`）
- `data/works/<entity>/*.md`：「做小作品」的成品（一份一檔）。檔名是「日期＋活動＋步驟」，
  同一天重做同一步會**覆蓋**同一個檔 ⇒ 舊筆記裡的〔作品：…〕打開看到的是那件作品的最新版。

`〔private〕` 段落一律不給（她的私記；`notes.read` 也不回流），只回「藏了幾段」。
"""

from __future__ import annotations

import datetime
import pathlib
import re
import threading

#: 筆記段落標頭：`19:45 「活動」／步驟…`（後面可能接 〔private〕／〔作品：…〕）
_HEAD = re.compile(r"^(\d{1,2}:\d{2})\s+「(.*?)」／(.*)$")
_WORK = re.compile(r"〔作品：([^〕]+)〕")
_ENTITY = re.compile(r"^[a-z0-9_-]{1,32}$")
MAX_DAYS = 31
MAX_BODY = 4000
#: 一天最多回幾步筆記（生活迴圈每幾分鐘寫一步 ⇒ 實測一天 888–1082 步、單日 JSON 約 1 MB；
#: 14 天全帶＝8 MB、七千多張卡片，手機端會直接卡死。只回**最近**這麼多步，其餘用 `notes_total` 交代。）
MAX_NOTES_PER_DAY = 30
#: 清單裡每一步正文的字數上限（App 只顯示 140 字；全文只在「作品」那條另外點開）。
PREVIEW_BODY = 280
#: 索引用的「那天有幾步」快取：`路徑 → (mtime_ns, size, 總步數, 藏了幾段)`。
#: 索引一次要數 14 天、每天 ~1 MB 的筆記檔；檔沒變（mtime＋大小一樣）就不再整檔解析。
_SUMMARY_CACHE: dict[str, tuple[int, int, int, int]] = {}
_SUMMARY_LOCK = threading.Lock()
_SUMMARY_MAX = 512


def _safe_entity(entity: str) -> str:
    entity = str(entity or "").strip().lower()
    return entity if _ENTITY.match(entity) else ""


def _parse_notes(text: str, *, limit: int = MAX_NOTES_PER_DAY,
                 preview: int = PREVIEW_BODY) -> tuple[list[dict], int, int]:
    """一天的筆記檔 → `([{time, activity, step, body, work}], 藏掉的私記段數, 總步數)`。

    只回**最近** `limit` 步（新的在後、取尾端再反轉成新的在前），正文截到 `preview` 字；
    總步數另外回，讓 App 說「ほか N 件」。
    """
    notes: list[dict] = []
    hidden = 0
    for block in text.split("\n## ")[1:]:
        head, _, body = block.partition("\n")
        if "〔private〕" in head or "〔private〕" in body.split("\n", 1)[0]:
            hidden += 1
            continue
        work = _WORK.search(head)
        head = _WORK.sub("", head).replace("　", " ").strip()
        match = _HEAD.match(head)
        notes.append({
            "time": match.group(1) if match else "",
            "activity": match.group(2) if match else head,
            "step": match.group(3).strip() if match else "",
            "body": body.strip()[:preview],
            "work": work.group(1).strip() if work else "",
        })
    total = len(notes)
    if limit and total > limit:
        notes = notes[-limit:]                     # 取最近幾步
    notes.reverse()                              # 新的在前（與原本的順序相反，因為檔案是時間正序）
    return notes, hidden, total


def _reflection(text: str) -> str:
    """`# 日期 日記\\n\\n內容` → 內容。"""
    lines = [line for line in text.splitlines() if not line.startswith("# ")]
    return "\n".join(lines).strip()[:MAX_BODY]


def _day_summary(notes_dir: pathlib.Path, day: str) -> tuple[int, int]:
    """那一天有幾步、藏了幾段（只數，不把正文帶出去）。"""
    note_file = notes_dir / f"{day}.md"
    try:
        if not note_file.is_file():
            return 0, 0
        info = note_file.stat()
    except OSError:
        return 0, 0
    key = str(note_file)
    with _SUMMARY_LOCK:
        hit = _SUMMARY_CACHE.get(key)
    if hit is not None and hit[0] == info.st_mtime_ns and hit[1] == info.st_size:
        return hit[2], hit[3]
    _notes, hidden, total = _parse_notes(note_file.read_text(encoding="utf-8", errors="replace"),
                                         limit=1, preview=1)
    with _SUMMARY_LOCK:
        if len(_SUMMARY_CACHE) >= _SUMMARY_MAX:
            _SUMMARY_CACHE.clear()
        _SUMMARY_CACHE[key] = (info.st_mtime_ns, info.st_size, total, hidden)
    return total, hidden


def diary(base, entity: str, *, days: int = 7, today: datetime.date | None = None) -> dict:
    """**索引**：最近 `days` 天（含今天）的日記與「那天有幾步」，新的在前；外加成品清單。

    ⚠ 這裡**不回每一步的正文**（2026-10-31：生活迴圈一天寫 888–1082 步，14 天全帶是 8 MB／七千多張卡，
    手機端一開就卡死）。要某一天的筆記另外打 `day()`；App 也是點開那一天才去拿、拿到就快取。
    """
    entity = _safe_entity(entity)
    if not entity:
        return {"ok": False, "error": "bad_entity"}
    base = pathlib.Path(base)
    days = max(1, min(MAX_DAYS, int(days or 7)))
    today = today or datetime.date.today()
    notes_dir = base / "notes" / entity
    out_days = []
    for offset in range(days):
        day = (today - datetime.timedelta(days=offset)).isoformat()
        total, hidden = _day_summary(notes_dir, day)
        reflection_file = notes_dir / f"reflection-{day}.md"
        reflection = (_reflection(reflection_file.read_text(encoding="utf-8", errors="replace"))
                      if reflection_file.is_file() else "")
        if total or reflection or hidden:
            out_days.append({"date": day, "reflection": reflection, "notes_total": total,
                             "private_hidden": hidden})
    return {"ok": True, "entity": entity, "days": out_days, "works": works(base, entity)}


def day(base, entity: str, date: str) -> dict | None:
    """某一天的筆記（最近 `MAX_NOTES_PER_DAY` 步、正文截到 `PREVIEW_BODY`）；日期格式不對回 None。"""
    entity = _safe_entity(entity)
    date = str(date or "").strip()
    if not entity or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return None
    try:
        datetime.date.fromisoformat(date)                 # 擋掉 2026-13-99 這種「格式對、日期不存在」
    except ValueError:
        return None
    base = pathlib.Path(base)
    notes_dir = base / "notes" / entity
    note_file = notes_dir / f"{date}.md"
    notes, hidden, total = ([], 0, 0)
    if note_file.is_file():
        notes, hidden, total = _parse_notes(note_file.read_text(encoding="utf-8", errors="replace"))
    reflection_file = notes_dir / f"reflection-{date}.md"
    reflection = (_reflection(reflection_file.read_text(encoding="utf-8", errors="replace"))
                  if reflection_file.is_file() else "")
    return {"ok": True, "entity": entity, "date": date, "reflection": reflection,
            "notes": notes, "notes_total": total, "private_hidden": hidden}


def works(base, entity: str, *, limit: int = 50) -> list[dict]:
    """成品清單（新的在前）：`[{name, date, title, step, chars}]`。"""
    entity = _safe_entity(entity)
    folder = pathlib.Path(base) / "works" / entity
    if not entity or not folder.is_dir():
        return []
    rows = []
    # 新的在前：照**修改時間**排，不是照檔名——檔名已經不帶時分（同名覆蓋），
    # 照檔名倒序會讓同一天的作品變成按字母排。
    def _mtime(path: pathlib.Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
    for path in sorted(folder.glob("*.md"), key=lambda item: (_mtime(item), item.name),
                       reverse=True)[:limit]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
        step = next((line[3:].strip() for line in text.splitlines() if line.startswith("## ")), "")
        rows.append({"name": path.name, "date": path.name[:10], "title": title, "step": step,
                     "chars": len(text)})
    return rows


def work(base, entity: str, name: str) -> dict | None:
    """一份成品的全文；名字只認成品資料夾裡**真的存在**的檔（不接受路徑）。"""
    entity = _safe_entity(entity)
    name = str(name or "")
    if not entity or "/" in name or "\\" in name or not name.endswith(".md"):
        return None
    folder = pathlib.Path(base) / "works" / entity
    path = folder / name
    if not folder.is_dir() or path.parent != folder or not path.is_file():
        return None
    return {"name": name, "text": path.read_text(encoding="utf-8", errors="replace")[:20000]}


__all__ = ["diary", "work", "works"]
