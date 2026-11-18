"""歷史來源清洗：把「本該是 Discord 私訊」的老回合標回 `discord:dm:`。

背景（owner 2026-10-29）
----------------------
修復前 `DiscordSource.handle()` 對私訊不標來源 ⇒ `routing_for()` 對非群聊一律預設
`phone:<principal>` ⇒ **私訊的履歷混進 App 的對話紀錄**，她在私訊裡還拿到 App 才有的
表情包工具。修復（`ca10bd1`）把私訊標成 `discord:dm:<principal>`。

⇒ 這裡處理**老資料**：讓 App 端的「對話紀錄」不再混進私訊。

判據（**只改能證明的**）
----------------------
`raw/<entity>/*.jsonl` 是逐字原文倉，帶 `source` 與 `scene`：
  · `scene == "discord-lobby"` ⇒ 一定是 Discord **群聊**（不是私訊）
  · `scene == "together"`   ⇒ App 打字 **∪** Discord **私訊**（舊私訊被 phone 化 ⇒ 同形）

⚠️ **舊私訊與舊 App 打字在資料上完全同形**（都是 `scene=together` + `source=phone:owner`）。
   沒有任何欄位能自動區分它們 ⇒ **工具只改「有獨立證據」的行，其餘列為待確認，不猜。**

獨立證據（可以改）：
  1. `raw` 裡同一輪的 `source` 已是 `discord:*`（例如群聊行）⇒ 與 outbox 不一致 ⇒ 依 raw 改正。
  2. `raw` 裡同一輪的 `scene` 是 `discord-lobby` ⇒ outbox 標 `phone:*` ⇒ 依 raw 改正為 `discord:*`。
  3. outbox 的 `source` 是空字串但 `raw` 有明確 `source` ⇒ 依 raw 補上（空值本來就不顯示）。

無法判定（**只報不改**）：
  · `scene=together` + `phone:*` 且 raw 也說 `phone:*` ⇒ 舊私訊與 App 打字同形，
    交給 owner 用他自己的記憶點名（`--mark-dm` / `--mark-app`）。

寫入前自動備份 `outbox/index.jsonl.bak-source-<ts>`。
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import pathlib
import re

#: 群聊的 scene（`raw` 裡 `discord:owner` 一定是它）
GROUP_SCENE = "discord-lobby"

#: 私訊的新標籤前綴
DM_PREFIX = "discord:dm:"

#: `discord:<principal>` → 群聊（owner 2026-10-27 的群聊走這條）
_DISCORD_GROUP_RE = re.compile(r"^discord:(?!dm:|vc\b)(.+)$")


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:                       # noqa: BLE001 - 壞行別擋整份
                pass
    return rows


def _raw_index(data: pathlib.Path) -> dict[tuple[str, str], list[dict]]:
    """(entity, text) → raw 裡那些回合（取值時挑時間最接近 outbox 的那次）。"""
    index: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    raw_dir = data / "raw"
    if not raw_dir.is_dir():
        return {}
    for ent_dir in sorted(raw_dir.glob("*")):
        if not ent_dir.is_dir():
            continue
        for path in sorted(ent_dir.glob("*.jsonl")):
            for row in _read_jsonl(path):
                if str(row.get("role")) != "user":
                    continue
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                index[(str(row.get("entity")), text)].append(row)
    return index


#: 判定「同一場對話」的時間窗（秒）
SEGMENT_GAP_S = 600.0


def _segments(rows: list[dict], *, gap: float = SEGMENT_GAP_S) -> list[list[dict]]:
    """把依時間排好的行切成「連續段」（相鄰間隔 < gap）。"""
    out: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        if current and float(row.get("at") or 0) - float(current[-1].get("at") or 0) > gap:
            out.append(current)
            current = []
        current.append(row)
    if current:
        out.append(current)
    return out


def _classify_segment(seg: list[dict]) -> str:
    """一段回合是「群聊 fan-out」「私訊」還是「App 打字」。

    ① **fan-out（群聊）**：同一句 `asked` 出現 ≥2 次且橫跨多個角色
       ⇒ Discord 群裡一條訊息，三個 bot 各回一輪。
    ② **私訊**：橫跨多個角色，但**句句不同**（owner 依序問不同 bot）。
       ⚠️ App 端一次只對一個角色說話 ⇒ 單一段內出現多個 entity 就不是 App 打字。
    ③ **App 打字**：整段只有一個角色。
    """
    entities = {str(row.get("entity")) for row in seg}
    asks = collections.Counter(
        str(row.get("asked") or "") for row in seg
        if str(row.get("asked") or "").strip())
    same_ask = asks.most_common(1)[0][1] if asks else 0
    if same_ask >= 2 and len(entities) > 1:
        return "group"
    if len(entities) > 1:
        return "dm"
    return "app"


def _guess_private_dm(rows: list[dict]) -> set[str]:
    """從 `phone:owner` 的行裡，推測哪些其實是 Discord 私訊（回傳 turn_id 集合）。

    ⚠️ **這是推測，不是證明** —— 同形（同角色、單句）的那些推不出來，仍要 owner 點名。
    判據見 `_classify_segment`。
    """
    targets = [r for r in rows
               if str(r.get("source") or "").startswith("phone:")
               and str(r.get("principal")) == "owner"]
    guessed: set[str] = set()
    for seg in _segments(targets):
        if _classify_segment(seg) != "dm":
            continue
        guessed.update(str(row.get("turn_id") or "") for row in seg)
    return guessed


def _pick(candidates: list[dict], at: float) -> dict | None:
    if not candidates:
        return None
    return min(candidates, key=lambda r: abs(float(r.get("at") or 0) - at))


def plan(data: pathlib.Path, *, guess_dm: bool = True,
          mark_dm: set[str] | None = None,
          mark_app: set[str] | None = None) -> dict:
    """算一遍會改什麼（不寫）。回傳統計與逐行決策。"""
    outbox = data / "outbox" / "index.jsonl"
    rows = _read_jsonl(outbox)
    index = _raw_index(data)
    #: 推測為私訊的行（跨角色 ⇒ 不是 App 打字）
    guessed = _guess_private_dm(rows) if guess_dm else set()
    #: owner 點名的行（點名比任何推斷都權威）
    dm_set = {str(x) for x in (mark_dm or ())}
    app_set = {str(x) for x in (mark_app or ())}

    decisions: list[dict] = []
    stats = collections.Counter()

    for row in rows:
        turn_id = str(row.get("turn_id") or "")
        source = str(row.get("source") or "")
        entity = str(row.get("entity") or "")
        asked = str(row.get("asked") or "").strip()
        at = float(row.get("at") or 0)

        candidates: list[dict] = index.get((entity, asked), [])
        raw = _pick(candidates, at)
        raw_source = str((raw or {}).get("source") or "")
        raw_scene = str((raw or {}).get("scene") or "")

        if raw is None:
            # ⚠️ raw 對拍不上**不影響 owner 點名**：owner 是用 App 端看到的原話指認的，
            #    那個判據比 raw 更權威（raw 可能根本沒落那一輪）。
            if turn_id in dm_set:
                # ⚠️ 標籤的主體是 **principal（說話的人）**，不是 entity（被叫的角色）。
                #    與修復後 `DiscordSource._source_for()` 一致：`discord:dm:{principal}`。
                principal = str(row.get("principal") or "owner")
                stats["owner_named_dm_no_raw"] += 1
                decisions.append({"turn_id": turn_id, "old": source,
                                  "new": f"{DM_PREFIX}{principal}",
                                  "action": "rewrite",
                                  "reason": "owner 點名為 Discord 私訊（raw 無對應回合）"})
                continue
            if turn_id in app_set:
                stats["owner_named_app_no_raw"] += 1
                decisions.append({"turn_id": turn_id, "old": source, "new": source,
                                  "action": "keep",
                                  "reason": "owner 點名為 App 打字（維持不變；raw 無對應回合）"})
                continue
            stats["no_raw_match"] += 1
            decisions.append({"turn_id": turn_id, "old": source, "new": None,
                              "action": "skip", "reason": "raw 找不到對應回合"})
            continue

        # ── 判據 1/2：raw 說它是**Discord** 群聊而 outbox 說 phone ⇒ 改 ──
        # ⚠️ 2026-11-07 REVIEW #1：scene 名不能單獨當判據。App 自己的群聊現在用
        # `scope=group:<conversation_id>`／`scene=app-group`（不再借用 discord-lobby），
        # 但舊資料裡可能還有 scene=discord-lobby 而來源其實是 App 的回合 ⇒
        # 只有「scene 是群聊場景 **而且** raw source 真的是 discord:*」才改標。
        if raw_scene == GROUP_SCENE and str(raw_source).startswith("discord:"):
            match = _DISCORD_GROUP_RE.match(raw_source)
            if match and not source.startswith("discord:"):
                stats["group_mislabelled"] += 1
                decisions.append({
                    "turn_id": turn_id, "old": source, "new": raw_source,
                    "action": "rewrite",
                    "reason": f"raw 判定為群聊（scene={GROUP_SCENE}，source={raw_source!r}）"
                              f" ⇒ outbox 的 {source!r} 是舊 phone 預設值",
                })
                continue
            stats["group_already_ok"] += 1
            decisions.append({"turn_id": turn_id, "old": source, "new": source,
                              "action": "keep", "reason": "群聊，標籤已正確"})
            continue

        # ── 判據 3：outbox 空標籤 ⇒ **不要補 phone 類** ──
        # ⚠️ App 端歷史只認 `phone`／`api` 前綴 ⇒ 空標籤的行**本來就不顯示**。
        #    補成 `phone:owner` 反而讓它**新出現在 App 歷史**（行為倒退）⇒ 不補。
        #    補成 `discord:*` 也不改變可見性（仍不匹配 phone/api）⇒ 純記錄，不寫。
        if not source:
            if raw_source.startswith("phone:") or raw_source.startswith("api:"):
                stats["empty_would_reveal"] += 1
                decisions.append({
                    "turn_id": turn_id, "old": source, "new": source, "action": "keep",
                    "reason": f"空標籤（App 本來不顯示）；raw 標為 {raw_source!r} ⇒ "
                              f"補上會讓它**新出現在 App 歷史**，不補",
                })
            else:
                stats["empty_filled_discord"] += 1
                decisions.append({
                    "turn_id": turn_id, "old": source, "new": raw_source,
                    "action": "rewrite",
                    "reason": f"空標籤 → raw 標為 {raw_source!r}（Discord 端；"
                              f"App 本來就不顯示，補上只為來源可追）",
                })
            continue

        # ── scene=together + phone ⇒ 舊私訊與 App 打字同形 ──
        if source.startswith("phone:") and raw_source.startswith("phone:"):
            if turn_id in guessed:
                # 跨角色 ⇒ 不可能是 App 打字（App 一次只對一個角色說話）
                # ⚠️ 標籤主體用 **principal**（說話的人），與 `_source_for()` 一致。
                principal = str(row.get("principal") or "owner")
                stats["guessed_dm"] += 1
                decisions.append({
                    "turn_id": turn_id, "old": source,
                    "new": f"{DM_PREFIX}{principal}",
                    "action": "rewrite",
                    "reason": "同一段對話裡橫跨多個角色 ⇒ 不可能是 App 打字"
                              "（App 一次只對一個角色說話）⇒ 判為 Discord 私訊",
                    "asked": asked, "at": at, "entity": entity,
                })
                continue
            stats["ambiguous_together_phone"] += 1
            if turn_id in dm_set:
                # owner 親自指認 ⇒ 直接改，不再列 review。
                # ⚠️ 標籤主體用 **principal**（說話的人），與 `_source_for()` 一致。
                principal = str(row.get("principal") or "owner")
                stats["owner_named_dm"] += 1
                decisions.append({
                    "turn_id": turn_id, "old": source,
                    "new": f"{DM_PREFIX}{principal}",
                    "action": "rewrite",
                    "reason": "owner 點名為 Discord 私訊",
                    "asked": asked, "at": at, "entity": entity,
                    "principal": principal,
                })
                continue
            if turn_id in app_set:
                # owner 點名為 App 打字 ⇒ 維持原樣（標籤不動）。
                stats["owner_named_app"] += 1
                decisions.append({
                    "turn_id": turn_id, "old": source, "new": source, "action": "keep",
                    "reason": "owner 點名為 App 打字（維持不變）",
                    "asked": asked, "at": at, "entity": entity,
                    "principal": str(row.get("principal") or "owner"),
                })
                continue
            decisions.append({
                "turn_id": turn_id, "old": source, "new": None, "action": "review",
                "reason": "scene=together 且 raw 也是 phone ⇒ 舊私訊與 App 打字同形，"
                          "無法自動判定（需 owner 點名）",
                "asked": asked, "at": at, "entity": entity,
                "principal": str(row.get("principal") or "owner"),
            })
            continue

        stats["unchanged"] += 1
        decisions.append({"turn_id": turn_id, "old": source, "new": source,
                          "action": "keep", "reason": "標籤一致"})

    return {"stats": dict(stats), "decisions": decisions, "rows": len(rows)}


def apply(data: pathlib.Path, *, dry_run: bool = True,
          mark_dm: list[str] | None = None,
          mark_app: list[str] | None = None,
          guess_dm: bool = True) -> dict:
    """把計畫套用。`mark_dm`／`mark_app` 是 owner 點名的 turn_id。"""
    data = pathlib.Path(data)
    dm_set = {str(x) for x in (mark_dm or [])}
    app_set = {str(x) for x in (mark_app or [])}
    if dm_set & app_set:
        raise ValueError(f"同一個 turn_id 不能同時標成 DM 與 App：{sorted(dm_set & app_set)}")

    result = plan(data, guess_dm=guess_dm, mark_dm=dm_set, mark_app=app_set)
    decisions = result["decisions"]

    extra = collections.Counter()

    rewrites = [d for d in decisions if d["action"] == "rewrite"]
    review = [d for d in decisions if d["action"] == "review"]

    if dry_run:
        return {"dry_run": True, "rows": result["rows"],
                "stats": result["stats"], "extra": dict(extra),
                "rewrite_count": len(rewrites), "review_count": len(review),
                "rewrites": rewrites, "review": review}

    # ── 真的寫 ──
    index_path = data / "outbox" / "index.jsonl"
    if not index_path.exists():
        return {"dry_run": False, "error": "outbox/index.jsonl 不存在", "rewritten": 0}

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = index_path.with_name(f"index.jsonl.bak-source-{stamp}")
    backup.write_bytes(index_path.read_bytes())

    by_id = {d["turn_id"]: d for d in rewrites}
    rows = _read_jsonl(index_path)
    changed = 0
    out_lines: list[str] = []
    for row in rows:
        item = by_id.get(str(row.get("turn_id") or ""))
        if item is not None:
            row["source"] = item["new"]
            changed += 1
        out_lines.append(json.dumps(row, ensure_ascii=False))
    index_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    return {"dry_run": False, "rows": len(rows), "rewritten": changed,
            "backup": str(backup), "stats": result["stats"], "extra": dict(extra),
            "review_count": len(review), "review": review}


__all__ = ["apply", "plan", "DM_PREFIX", "GROUP_SCENE"]
