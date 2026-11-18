#!/usr/bin/env python3
"""改正舊的封片 id（補上線後綴）：python3 agent/tools/backfill_episode_ids.py --data data [--apply]

為什麼要它：`new_episode_id` 原本是 `ep_<entity>-<線內序號>`，而序號是**每條線自己的**
（`thread.episode_index`）⇒ 每一條線的第 1 集都叫 `ep_mira-1`。實測 `episodes.jsonl`
583 筆只有 76 個唯一 id（`ep_mira-1` 出現 63 次），載入按 id 去重就把歷史吃掉了。
2026-10-27 起新封的片帶線後綴（`ep_mira-1-th_7f629aa0c468_1`），這一支負責把舊檔改成同一格式。

改哪兩處（實測引用鏈）：
  ① `data/episodes.jsonl` 的 `id`；
  ② `data/raw/<entity>/<YYYY-MM>.jsonl` 每一輪的 `episode` 欄位
     （實測 mira 949 行、nori 128 行；aster 0 行）。
  ※ `data/affect/*/app/state.json` 實測 0 個 `ep_` 引用；一旦掃到引用就**停下來**（fail-closed，
    真的要改得自己帶 `--force`）。

判據：
  · 新 id = `<舊 id>-<線後綴>`，後綴規則與新封的片**共用** `domain/ids.id_fragment`（不各寫一份）；
  · 同一條線、同一個舊 id 卻有兩個開始時刻（實測 2 組：`ep_mira-15`、`ep_nori-14`）
    ⇒ 再補開始時刻的短哈希，才真的唯一；
  · 已經是新格式（含線後綴，可帶短哈希）⇒ 不動，**重跑安全**；
  · raw 行的引用用（舊 id, 線）定位；同鍵多筆時看「這一行落在哪一集的時間範圍」，仍然分不出
    ⇒ 跳過並計數（**不猜**）；
  · raw 只改 `"episode": "<舊>"` 這一段字串，**其餘逐字不動**（原文是逐字權威檔，不是重寫 JSON）；
  · `--apply` 會先**整套預跑一次**（不寫）：raw 裡有解析不了的引用（unknown／ambiguous）⇒
    一個檔都不寫，回 3。以前是先寫封片檔、再處理 raw ⇒ 歧義引用留在舊 id，
    而重跑時候選表只剩新 id，舊引用從 ambiguous 變 unknown，**再也接不回去**（review 2026-10-28）。
    確認那些引用可以放棄，才帶 `--allow-unresolved`。

寫入前自動備份 `episodes.jsonl.bak-id-<ts>`、`raw/<entity>/<月>.jsonl.bak-id-<ts>`，再原子替換；
寫完逐筆自核（封片：除了 `id` 以外每一欄與備份相同；raw：除了 `episode` 以外每一行逐字相同）。

★ 執行時機：**服務停止時**（raw 與 affect 都由 runtime 寫）。
    docker compose stop
    docker run --rm -v $PWD/data:/app/data --entrypoint python3 blueagent-next:latest \\
        tools/backfill_episode_ids.py --data /app/data --apply
    docker compose start
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))

from agent.brain.domain.ids import START_HASH_LEN, id_fragment  # noqa: E402
from agent.brain.domain.ids import start_hash as short_hash  # noqa: E402

_HEX = set("0123456789abcdef")
_HASH_LEN = START_HASH_LEN


def is_migrated(episode_id: str, fragment: str) -> bool:
    """這個 id 是不是已經是「帶線後綴」的新格式（可帶 `-<短哈希>`）。"""
    if not fragment:
        return False
    if episode_id.endswith(f"-{fragment}"):
        return True
    head, _, tail = episode_id.rpartition("-")
    return len(tail) == _HASH_LEN and set(tail) <= _HEX and head.endswith(f"-{fragment}")


def base_id(episode_id: str, fragment: str) -> str:
    """把「線後綴」與「開始時刻短哈希」剝掉，回到 `ep_<entity>-<index>`。"""
    if not fragment:
        return episode_id
    head, _, tail = episode_id.rpartition("-")
    if len(tail) == _HASH_LEN and set(tail) <= _HEX and head.endswith(f"-{fragment}"):
        episode_id = head
    if episode_id.endswith(f"-{fragment}"):
        return episode_id[: -(len(fragment) + 1)]
    return episode_id


def plan_ids(episodes: list[dict], *, hash_all: bool = False) -> tuple[list[str], dict, dict]:
    """算出每一筆的新 id。

    回 `(new_ids, candidates, stats)`：
      · `new_ids[i]` 是第 i 筆的新 id（不需要改時等於舊的）；
      · `candidates[(舊 id, 線)] = [(新 id, 開始, 結束)]`，給 raw 行按時間定位用。

    `hash_all=True` 連「沒有撞車的組」也補開始時刻短哈希（舊檔格式統一用；
    運行時 `new_episode_id` 一律帶哈希，所以這才是跟新封的片同一個形狀）。
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for index, record in enumerate(episodes):
        key = (str(record.get("id") or ""), str(record.get("thread") or ""))
        groups.setdefault(key, []).append(index)
    new_ids = [str(record.get("id") or "") for record in episodes]
    # 候選以（新 id, 開始時刻）為鍵：同一集被改寫時留**最晚**的結束時刻（跟載入同一口徑），
    # 否則「同 id 同線同開始、只有 ended_at 不同」的改寫版會被當成兩集 ⇒ raw 行無法定位。
    candidates: dict[tuple[str, str], dict[tuple[str, float], tuple[str, float, float]]] = {}
    stats = {"records": len(episodes), "change": 0, "already": 0, "no_thread": 0,
             "empty_id": 0, "collision_groups": 0}
    for (old, thread), indexes in groups.items():
        fragment = id_fragment(thread)
        if not old:
            stats["empty_id"] += len(indexes)
            continue
        if not fragment:
            stats["no_thread"] += len(indexes)
            continue
        starts = {float(episodes[index].get("started_at") or 0.0) for index in indexes}
        collide = len(starts) > 1
        if collide:
            stats["collision_groups"] += 1
        head = base_id(old, fragment)
        for index in indexes:
            record = episodes[index]
            if hash_all or collide:                   # 同線同 id 兩集 ⇒ 一定要哈希才分得開
                target = f"{head}-{fragment}-{short_hash(record.get('started_at'))}"
            elif base_id(old, fragment) != old:
                target = old                          # 已經帶線後綴：不動，也不主動拆哈希
            else:
                target = f"{head}-{fragment}"
            if target == old:
                stats["already"] += 1                 # 重跑安全：已經是這個格式
            else:
                new_ids[index] = target
                stats["change"] += 1
            start = float(record.get("started_at") or 0.0)
            end = float(record.get("ended_at") or 0.0)
            bucket = candidates.setdefault((old, thread), {})
            place = (new_ids[index], start)
            if place not in bucket or end > bucket[place][2]:
                bucket[place] = (new_ids[index], start, end)
    flat = {key: list(bucket.values()) for key, bucket in candidates.items()}
    return new_ids, flat, stats


def resolve_raw(episode_id: str, thread: str, at: float,
                candidates: dict) -> tuple[str, str]:
    """raw 一行的 `episode` → 新 id。回 `(新 id, 狀態)`；狀態非空＝沒改（already/unknown/ambiguous）。"""
    known = {new for rows in candidates.values() for new, _, _ in rows}
    if episode_id in known:
        return episode_id, "already"
    rows = candidates.get((episode_id, thread)) or []
    if not rows:
        return episode_id, "unknown"
    if len(rows) == 1:
        return rows[0][0], ""
    inside = [new for new, start, end in rows if start <= at <= end]
    if len(inside) == 1:
        return inside[0], ""
    return episode_id, "ambiguous"


def rewrite_episode_field(line: str, old: str, new: str) -> str:
    """只換 `"episode": "<舊>"` 這一段，其餘逐字不動。換不到或不唯一就回原字串。"""
    pattern = re.compile(r'("episode"\s*:\s*")' + re.escape(old) + r'(")')
    if len(pattern.findall(line)) != 1:
        return line
    return pattern.sub(lambda match: match.group(1) + new + match.group(2), line, count=1)


def rewrite_id_field(line: str, old: str, new: str) -> str:
    """只換封片檔這一行的 `"id": "<舊>"`。"""
    pattern = re.compile(r'("id"\s*:\s*")' + re.escape(old) + r'(")')
    if len(pattern.findall(line)) != 1:
        return line
    return pattern.sub(lambda match: match.group(1) + new + match.group(2), line, count=1)


def _backup(path: pathlib.Path, stamp: str) -> pathlib.Path:
    target = path.with_name(f"{path.name}.bak-id-{stamp}")
    target.write_bytes(path.read_bytes())
    return target


def _write_atomic(path: pathlib.Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp-id")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _read_lines(path: pathlib.Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def backfill_episodes(path: pathlib.Path, *, apply: bool, stamp: str,
                      hash_all: bool = False) -> tuple[int, dict]:
    """改封片檔的 id。回 `(改了幾個 id, stats)`。"""
    lines = _read_lines(path)
    records: list[dict] = []
    keep: list[int] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
            keep.append(index)
    new_ids, candidates, stats = plan_ids(records, hash_all=hash_all)
    changed = 0
    out = list(lines)
    for slot, index in enumerate(keep):
        old, new = str(records[slot].get("id") or ""), new_ids[slot]
        if not new or new == old:
            continue
        replaced = rewrite_id_field(out[index], old, new)
        if replaced == out[index]:
            stats["id_field_not_found"] = stats.get("id_field_not_found", 0) + 1
            continue
        out[index] = replaced
        changed += 1
    if apply and changed:
        _backup(path, stamp)
        _write_atomic(path, "\n".join(out) + "\n")
        _verify_episodes(path, records, keep, new_ids)
    return changed, {**stats, "candidates": candidates}


def _verify_episodes(path: pathlib.Path, records: list[dict], keep: list[int],
                     new_ids: list[str]) -> None:
    """自核：筆數不變、每筆除了 id 以外與改前相同、新 id 不再跨線／跨時段撞。

    同一集的**改寫版**（同線、同開始時刻、只有 ended_at 不同）本來就該共用一個 id，
    所以判據不是「id 全唯一」，而是「一個 id 只對應一組（線, 開始時刻）」。
    """
    lines = _read_lines(path)
    after: list[dict] = []
    for index in keep:
        after.append(json.loads(lines[index]))
    if len(after) != len(records):
        raise SystemExit(f"封片筆數變了：{len(records)} → {len(after)}")
    for before, current, wanted in zip(records, after, new_ids):
        if current.get("id") != wanted:
            raise SystemExit(f"id 沒寫進去：{before.get('id')} → {current.get('id')}")
        left = {key: value for key, value in before.items() if key != "id"}
        right = {key: value for key, value in current.items() if key != "id"}
        if left != right:
            raise SystemExit(f"除了 id 以外有欄位被動到：{before.get('id')}")
    shared: dict[str, set] = {}
    for row in after:
        shared.setdefault(str(row.get("id") or ""), set()).add(
            (row.get("thread"), row.get("started_at")))
    collide = {key: value for key, value in shared.items() if len(value) > 1}
    if collide:
        sample = list(collide.items())[0]
        raise SystemExit(f"改完仍有 {len(collide)} 個 id 跨線／跨時段重複（例：{sample}）")


def backfill_raw(path: pathlib.Path, candidates: dict, *, apply: bool,
                 stamp: str) -> dict:
    """改 raw 月檔每一行的 `episode`。回 stats。"""
    stats = {"rows": 0, "refs": 0, "change": 0, "already": 0, "unknown": 0, "ambiguous": 0}
    lines = _read_lines(path)
    out = list(lines)
    for index, line in enumerate(lines):
        stats["rows"] += 1
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not row.get("episode"):
            continue
        stats["refs"] += 1
        new, state = resolve_raw(str(row["episode"]), str(row.get("thread") or ""),
                                 float(row.get("at") or 0.0), candidates)
        if state:
            stats[state] += 1
            continue
        replaced = rewrite_episode_field(line, str(row["episode"]), new)
        if replaced == line:
            stats["ambiguous"] += 1                   # 欄位找不到（格式怪）⇒ 不猜
            continue
        out[index] = replaced
        stats["change"] += 1
    if apply and stats["change"]:
        _backup(path, stamp)
        _write_atomic(path, "\n".join(out) + "\n")
        for index, line in enumerate(lines):
            if out[index] == line:
                continue
            before, after = json.loads(line), json.loads(out[index])
            left = {key: value for key, value in before.items() if key != "episode"}
            right = {key: value for key, value in after.items() if key != "episode"}
            if left != right:
                raise SystemExit(f"raw 除了 episode 以外被動到：{path.name} 第 {index + 1} 行")
    return stats


def scan_affect_refs(root: pathlib.Path) -> tuple[int, int, int]:
    """掃 affect 狀態檔裡的 `ep_` 引用。回 `(有幾個檔, 讀不到的檔, 幾個引用)`。

    讀不到（權限）要**算出來報**，不然「掃了 0 個檔」會被讀成「沒有引用」——本機的
    `affect/<entity>/app/state.json` 是容器以 root 寫的，非 root 跑這支時就讀不到。
    """
    files = unreadable = hits = 0
    for path in sorted((root / "affect").glob("*/app/state.json")):
        files += 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            unreadable += 1
            continue
        hits += len(re.findall(r"ep_[a-z0-9]+-\d+", text))
    return files, unreadable, hits


def _raw_totals(root: pathlib.Path, candidates: dict, *, apply: bool, stamp: str,
                report: bool) -> dict:
    """逐檔跑 `backfill_raw`，回合計；`report` 才印每個檔那一行。"""
    total = {"refs": 0, "change": 0, "already": 0, "unknown": 0, "ambiguous": 0}
    for path in sorted((root / "raw").glob("*/*.jsonl")):
        row = backfill_raw(path, candidates, apply=apply, stamp=stamp)
        if report and row["refs"]:
            print(f"raw/{path.parent.name}/{path.name}: rows={row['rows']} refs={row['refs']} "
                  f"change={row['change']} already={row['already']} "
                  f"unknown={row['unknown']} ambiguous={row['ambiguous']}")
        for key in total:
            total[key] += row[key]
    return total


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把舊封片 id 補上線後綴（唯一化）")
    parser.add_argument("--data", default=str(_HERE.parents[1] / "data"))
    parser.add_argument("--apply", action="store_true", help="真的寫入（預設只出報表）")
    parser.add_argument("--force", action="store_true",
                        help="affect 狀態檔掃到 ep_ 引用時仍要寫（預設停下來）")
    parser.add_argument("--allow-unresolved", action="store_true",
                        help="raw 有解析不了的引用（unknown／ambiguous）仍要寫（預設整套不寫）")
    parser.add_argument("--hash-all", action="store_true",
                        help="連沒撞車的組也補開始時刻短哈希（把舊檔格式統一成跟新封的片一樣）")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.data)
    episodes = root / "episodes.jsonl"
    if not episodes.is_file():
        print(f"沒有 {episodes}")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    files, unreadable, refs = scan_affect_refs(root)
    if not args.force and (refs or (unreadable and args.apply)):
        print(f"affect 狀態檔：{files} 個檔、其中 {unreadable} 個讀不到、掃到 {refs} 個 ep_ 引用 ⇒ 停下來。"
              f"先確認那些引用怎麼跟著改（讀不到就用 root 跑），或帶 --force。")
        return 2

    if args.apply and not args.allow_unresolved:
        # 預跑（不寫）：封片檔一旦改成新 id，留在 raw 的舊引用就失去對照表 ⇒ 要嘛全改、要嘛不動
        _, preview = backfill_episodes(episodes, apply=False, stamp=stamp, hash_all=args.hash_all)
        pending = _raw_totals(root, preview["candidates"], apply=False, stamp=stamp, report=False)
        unresolved = pending["unknown"] + pending["ambiguous"]
        if unresolved:
            print(f"raw 有 {unresolved} 個引用解析不了（unknown={pending['unknown']} "
                  f"ambiguous={pending['ambiguous']}）⇒ 一個檔都沒寫。先不帶 --apply 看報表；"
                  f"確認可以放棄那些引用再帶 --allow-unresolved。")
            return 3
    changed, stats = backfill_episodes(episodes, apply=args.apply, stamp=stamp,
                                       hash_all=args.hash_all)
    candidates = stats.pop("candidates")
    print(f"episodes.jsonl: records={stats['records']} change={changed} "
          f"already={stats['already']} no_thread={stats['no_thread']} "
          f"empty_id={stats['empty_id']} collision_groups={stats['collision_groups']}"
          + (f" id_field_not_found={stats['id_field_not_found']}"
             if stats.get("id_field_not_found") else ""))
    total = _raw_totals(root, candidates, apply=args.apply, stamp=stamp, report=True)
    print(f"raw 合計: refs={total['refs']} change={total['change']} "
          f"already={total['already']} unknown={total['unknown']} ambiguous={total['ambiguous']}")
    print(f"affect 狀態檔: {files} 個檔（{unreadable} 個讀不到）、{refs} 個 ep_ 引用")
    touched = changed + total["change"]
    if not args.apply:
        print("（dry-run，未寫入）" if touched else "（沒有東西要改）")
    else:
        print(f"已寫入 {touched} 處（備份檔後綴 .bak-id-{stamp}）" if touched
              else "沒有東西要改（已是新格式）")
    return 1 if (total["unknown"] or total["ambiguous"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
