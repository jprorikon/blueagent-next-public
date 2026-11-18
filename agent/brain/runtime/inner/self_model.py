"""自傳式記憶（提案 ⑤，owner 2026-11-16「帮我做了」）。

`data/self/<entity>/self-model.md` 兩段：

```markdown
# <entity> の自己像（自動生成；每行都必須帶出處）

## 我确实是这样的人
- 主人のことは「オーナー」と呼ぶ〔fact:1821〕
- 記録は通し番号から書く（実験ノートの記事から）〔fact:9042〕

## 最近的我
- TRPG の判定表をまとめ直した〔fact:19877〕
```

三條規則（寫在代碼裡，不靠人記）：

1. **每行都要帶出處**：`〔fact:<id>〕`。沒有 id 的行直接丟掉——寧可少一行，也不寫沒有依據的自述。
2. **被推翻就走 supersede**：重建時只讀 `superseded_by` 為空的事實，所以舊自述自然消失，
   不需要「改寫」或「刪除」這一步（與 `memory-supersede-pointers.md` 同一套語意）。
3. **紅線：不寫人格卡**。`soul`／`core.json` 歸 owner；這一支只產出「可引用的自述」，
   兩個消費點是**自主任務的 prompt**（`compose/life_wiring` 的 context_block）與**內心獨白**。

重建是**確定性**的（不叫模型）：分數＝層權重＋時間衰減，取前幾條；同樣的庫重建出的結果一樣。
判據（PENDING §A.5）：三個月後答得出「她這期間偏好變了什麼」——所以「最近的我」那一段按時間
排序、保留到最近 7 天。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import time

STABLE_HEADER = "## 我确实是这样的人"
RECENT_HEADER = "## 最近的我"
#: 這一段最多幾行（兩段各自）
DEFAULT_LIMIT = 6
#: 「最近的我」看最近幾天
RECENT_DAYS = 7
#: 注入 prompt 的字數上限（自主任務／獨白共用）
PROMPT_MAX_CHARS = 700
#: 穩定段收哪些 kind（狀態、稱呼、偏好、規則）；event 型是她自己的流水，只進「最近的我」
STABLE_KINDS = ("state", "preference", "rule", "identity", "称呼", "称呼槽")
#: 穩定段的層權重（關係層優先，自己的流水最後）
LAYER_WEIGHT = {"private": 1.0, "pair": 1.0, "circle": 0.7, "consensus": 0.6, "self": 0.3, "": 0.4}
#: 主人的 principal（legacy `owner` 與平台帳號 `acct_owner` 都算）——自述只收「她」或「她＋主人」的房間
from agent.brain.memory.facts import HOSTS  # noqa: E402 - 與私記降權同一組


def _root(root) -> pathlib.Path:
    return pathlib.Path(root) / "self"


def path_of(root, entity: str) -> pathlib.Path:
    """`data/self/<entity>/self-model.md`。"""
    slug = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in str(entity or "")).strip("-")
    return _root(root) / (slug or "unknown") / "self-model.md"


def _score(row: dict, now: float) -> float:
    at = float(row.get("created_at") or 0.0)
    age_days = max(0.0, (now - at) / 86400.0)
    recency = 0.2 + 0.8 * (0.5 ** (age_days / 30.0))
    return LAYER_WEIGHT.get(str(row.get("layer") or ""), 0.4) * recency


def _line(row: dict, limit: int = 120) -> str:
    text = " ".join(str(row.get("text") or "").split())
    if not text:
        return ""
    return f"- {text[:limit]}〔fact:{row.get('id')}〕"


def _gist(text: str) -> str:
    """同一件事的多步流水（「…」1/3 完了：…）折成一行：去掉 `2/3` 這種步號、取活動標題當鍵。"""
    head = " ".join(str(text or "").split())
    head = re.sub(r"\d+\s*/\s*\d+", "", head)          # 2/3、1/3 ⇒ 去掉
    if "」" in head[:60]:                              # 「活動名」後面的細節不算鍵
        head = head.split("」")[0] + "」"
    for mark in ("：", ":", "—"):
        if mark in head[:60]:
            head = head.split(mark)[0]
            break
    return head[:24]


def eligible(audience, entity: str, *, hosts=HOSTS) -> bool:
    """這條事實能不能進她的自述（2026-11-16 審查返修）。

    自述的兩個消費點（自主任務、獨白）都是她一個人想事情、說出口的對象是主人
    ⇒ 只收**房間只有她**或**只有她＋主人**的事實：
      · 世界事實（`audience=None`）不是自述；
      · 有第三人的房間（她和アスター的私事、三人的圈子）不收——那是別人的事，
        進了獨白就可能順著 talk 動機說給主人聽。
    """
    if audience is None:
        return False
    members = {str(who).strip() for who in audience if str(who).strip()}
    me = str(entity or "").strip()
    if not members:
        return False
    return members <= ({me} | set(hosts))


def _audience_of(raw) -> set[str] | None:
    """`facts.audience` 欄（逗號串接；NULL＝世界）。"""
    if raw is None:
        return None
    return {item.strip() for item in str(raw).split(",") if item.strip()}


def _rows_from_db(db_path, entity: str, *, days: int = 400, now: float | None = None,
                  hosts=HOSTS) -> list[dict]:
    """她的自述素材：只讀**現實**（`game=0`）、仍有效（沒被取代、沒過期）、房間合格的事實。"""
    moment = float(now if now is not None else time.time())
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        rows = db.execute(
            "SELECT id, text, kind, layer, source, created_at, audience FROM facts "
            "WHERE entity = ? AND (superseded_by IS NULL OR superseded_by = '') "
            "AND COALESCE(game, 0) = 0 "                      # 桌上的事不是她（2026-11-11 B4）
            "AND COALESCE(fiction, 0) = 0 "                   # 她寫的作品不是她的經歷（2026-11-16）
            "AND (valid_to IS NULL OR valid_to = 0 OR valid_to > ?) "
            "AND created_at >= ? ORDER BY created_at DESC LIMIT 1200",
            (str(entity), moment, moment - days * 86400.0)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        db.close()
    return [{"id": row["id"], "text": row["text"], "kind": str(row["kind"] or ""),
             "layer": str(row["layer"] or ""), "source": str(row["source"] or ""),
             "created_at": float(row["created_at"] or 0.0)} for row in rows
            if eligible(_audience_of(row["audience"]), entity, hosts=hosts)]


def build(rows: list[dict], *, now: float | None = None,
          limit: int = DEFAULT_LIMIT) -> tuple[list[str], list[str]]:
    """回 `(穩定段的行, 最近段的行)`；每行都帶 `〔fact:id〕`。"""
    moment = float(now if now is not None else time.time())
    stable: list[tuple[float, str]] = []
    recent: list[tuple[float, str]] = []
    for row in rows or ():
        if str(row.get("superseded_by") or ""):
            continue                     # 規則 2：被取代的事實不進自述（不靠改寫／刪除）
        if int(row.get("fiction") or 0):
            continue                     # 她寫的作品不是她（owner 2026-11-16）
        line = _line(row)
        if not line:
            continue
        kind = str(row.get("kind") or "")
        if any(word in kind for word in STABLE_KINDS):
            stable.append((_score(row, moment), line))
        elif moment - float(row.get("created_at") or 0.0) <= RECENT_DAYS * 86400.0:
            recent.append((float(row.get("created_at") or 0.0), line))
    stable.sort(key=lambda item: item[0], reverse=True)
    recent.sort(key=lambda item: item[0], reverse=True)
    # 「最近的我」是自述不是流水：同一件事的 1/3、2/3、3/3 只留最新那條
    seen: set[str] = set()
    recent_lines: list[str] = []
    for _at, line in recent:
        key = _gist(line)
        if key in seen:
            continue
        seen.add(key)
        recent_lines.append(line)
        if len(recent_lines) >= limit:
            break
    return ([line for _score_value, line in stable[:limit]], recent_lines)


def render(entity: str, stable: list[str], recent: list[str], *,
           now: float | None = None) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(now if now is not None else time.time())))
    body = [f"# {entity} の自己像（自動生成；每行都帶出處。2026-11-16 起）",
            f"> 重建時刻 {stamp}　只讀 `superseded_by` 為空的事實；紅線：不寫人格卡。", ""]
    body.append(STABLE_HEADER)
    body.extend(stable or ["- （まだ無い）"])
    body.append("")
    body.append(RECENT_HEADER)
    body.extend(recent or ["- （まだ無い）"])
    return "\n".join(body) + "\n"


def rebuild(root, entity: str, *, db_path=None, rows=None, now: float | None = None,
            limit: int = DEFAULT_LIMIT) -> dict:
    """重建一份並落檔（確定性；沒有事實就寫「まだ無い」，不編造）。"""
    source = rows if rows is not None else (_rows_from_db(db_path, entity, now=now) if db_path else [])
    stable, recent = build(source, now=now, limit=limit)
    target = path_of(root, entity)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render(entity, stable, recent, now=now)
    temporary = target.with_suffix(".md.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)
    return {"entity": entity, "path": str(target), "stable": stable, "recent": recent,
            "lines": len(stable) + len(recent), "at": float(now if now is not None else time.time())}


def read(root, entity: str) -> dict:
    """讀回已生成的那份（兩段各自的行）。"""
    target = path_of(root, entity)
    try:
        body = target.read_text(encoding="utf-8")
    except OSError:
        return {"entity": entity, "stable": [], "recent": [], "exists": False}
    stable: list[str] = []
    recent: list[str] = []
    bucket = None
    for line in body.splitlines():
        if line.startswith(STABLE_HEADER):
            bucket = stable
            continue
        if line.startswith(RECENT_HEADER):
            bucket = recent
            continue
        if bucket is not None and line.startswith("- "):
            if "〔fact:" in line:            # 規則 1：沒有出處的行不進 prompt
                bucket.append(line[2:].strip())
    return {"entity": entity, "stable": stable, "recent": recent, "exists": True,
            "path": str(target)}


def prompt_block(root, entity: str, *, max_chars: int = PROMPT_MAX_CHARS) -> str:
    """給 prompt 用的一段（沒有檔案就回空字串——不要空標題）。"""
    data = read(root, entity)
    stable, recent = data.get("stable") or [], data.get("recent") or []
    if not stable and not recent:
        return ""
    lines = ["［自分のこと（自動生成の自己像。出処の id 付き）］"]
    if stable:
        lines.append("・ずっとそういう人間：" + "／".join(item.split("〔")[0].strip() for item in stable[:4]))
    if recent:
        lines.append("・最近の自分：" + "／".join(item.split("〔")[0].strip() for item in recent[:3]))
    return "\n".join(lines)[:max_chars]


__all__ = ["DEFAULT_LIMIT", "HOSTS", "PROMPT_MAX_CHARS", "RECENT_HEADER", "STABLE_HEADER", "build", "eligible",
           "path_of", "prompt_block", "read", "rebuild", "render"]
