"""回填**承接段**的門牌：把舊封片留下的 `Thread.carry_summary` 補上指針。

為什麼要它：門牌是 2026-10-25 才進 `EpisodeArchive.seal()` 的。之前的承接段是同一批
`data/episodes.jsonl` 摘要的複本——只是**沒帶指針**。它們不是「憑空猜」，而是**同一份產物**：
所以判據只能是**逐字相等**：

    ① 承接段去掉可能的 `〔raw:…〕` 前綴後，與某條 episode 的 summary（同樣去前綴）**字串完全相等**；
    ② 該 episode 在 `episodes.jsonl` 裡帶著 `turns`（每輪有 `turn` id）⇒ 指針由它推出來；
    ③ 找不到、或找到多條 ⇒ **跳過不寫**（寧可沒有門牌，也不要錯的門牌）；
    ④ 跨線比對只認**同一個角色**的 episode（線的角色在 `key.entity`）；角色不明 ⇒ 跳過；
    ⑤ 已經帶門牌的承接段也照同一條規則重算：2026-10-26 前 `+n` 數的是原文行（約翻倍），
       重算值不同就換掉（`corrected`）；對不到 episode 的保留原樣、記 `unverified`。

★ 只在**服務停止時**跑（或當一次性 job 跑同一個 image）：`threads.json` 由 runtime 持有並持久化，
  邊跑邊改會互相覆蓋。
"""

from __future__ import annotations

import json
import pathlib
import re
import time

from agent.brain.runtime.memory.pointer import make

_PREFIX = re.compile(r"^〔raw:[^〕]*〕\s*")


def strip_prefix(text: object) -> str:
    return _PREFIX.sub("", str(text or "")).strip()


def _episode_pointer(episode: dict) -> str:
    entity = str(episode.get("entity") or "")
    ids = [str(turn.get("turn") or "") for turn in (episode.get("turns") or ())
           if isinstance(turn, dict)]
    return make(entity, ids)


def _thread_entity(thread: dict) -> str:
    key = thread.get("key")
    return str(key.get("entity") or "").strip() if isinstance(key, dict) else ""


def _match(thread: dict, body: str, by_thread: dict, by_text: dict) -> tuple[str, list[dict]]:
    """這段承接段是哪一條 episode 的複本 ⇒ `(stat_key, candidates)`；`stat_key` 空＝有候選。"""
    same_thread = [ep for ep in by_thread.get(str(thread.get("id") or ""), [])
                   if strip_prefix(ep.get("summary")) == body]
    if same_thread:
        # 同一條線封過好幾次、摘要文字相同 ⇒ 承接段是**最近那次**（不是猜：carry 就是最後一次封片的產物）
        return "", [max(same_thread, key=lambda ep: float(ep.get("ended_at") or 0))]
    entity = _thread_entity(thread)
    if not entity:
        return "skipped_no_entity", []
    # 跨線只認同一個角色：別人的 episode 給出的是別人倉裡的門牌，`memory.open` 永遠打不開
    candidates = [ep for ep in by_text.get(body, [])
                  if str(ep.get("entity") or "").strip() == entity]
    if not candidates:
        return "skipped_no_match", []
    if len(candidates) > 1:
        return "skipped_ambiguous", []
    return "", candidates


def plan(threads: list[dict], episodes: list[dict]) -> tuple[list[dict], dict]:
    """算出「哪些承接段可以補（或改正）門牌」。回 `(changes, stats)`；`changes` 只在記憶體裡。"""
    by_thread: dict[str, list[dict]] = {}
    by_text: dict[str, list[dict]] = {}
    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        body = strip_prefix(episode.get("summary"))
        if not body:
            continue
        by_thread.setdefault(str(episode.get("thread") or ""), []).append(episode)
        by_text.setdefault(body, []).append(episode)

    changes: list[dict] = []
    stats = {"threads": 0, "carry": 0, "already": 0, "filled": 0, "corrected": 0,
             "unverified": 0, "skipped_no_match": 0, "skipped_ambiguous": 0,
             "skipped_no_turns": 0, "skipped_no_entity": 0}
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        stats["threads"] += 1
        carry = str(thread.get("carry_summary") or "").strip()
        if not carry:
            continue
        stats["carry"] += 1
        prefixed = _PREFIX.match(carry)
        current = prefixed.group(0).strip()[1:-1] if prefixed else ""
        body = strip_prefix(carry)
        miss, candidates = _match(thread, body, by_thread, by_text)
        ptr = _episode_pointer(candidates[0]) if candidates else ""
        if not ptr:
            # 已帶門牌但對不到（或推不出）⇒ 保留原樣，但不算「確認過」
            stats["unverified" if current else (miss or "skipped_no_turns")] += 1
            continue
        if ptr == current:
            stats["already"] += 1
            continue
        changes.append({"thread": str(thread.get("id") or ""), "ptr": ptr,
                        "carry": f"〔{ptr}〕\n{body}"})
        stats["corrected" if current else "filled"] += 1
    return changes, stats


def apply(data_dir, *, dry_run: bool = True, clock=time.time) -> dict:
    """讀 `data/threads.json` ＋ `data/episodes.jsonl`，把可補的承接段補上門牌。"""
    data = pathlib.Path(data_dir)
    threads_path = data / "threads.json"
    episodes_path = data / "episodes.jsonl"
    body = json.loads(threads_path.read_text(encoding="utf-8"))
    threads = body.get("threads") if isinstance(body, dict) else body
    if not isinstance(threads, list):
        raise ValueError("threads.json 形狀不對（沒有 threads 陣列）")
    episodes: list[dict] = []
    if episodes_path.exists():
        for line in episodes_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                episodes.append(record)
    changes, stats = plan(threads, episodes)
    if changes and not dry_run:
        backup = threads_path.with_suffix(f".json.bak-carry-{time.strftime('%Y%m%d-%H%M%S', time.localtime(clock()))}")
        backup.write_text(threads_path.read_text(encoding="utf-8"), encoding="utf-8")
        by_thread = {item["thread"]: item for item in changes}
        for thread in threads:
            item = by_thread.get(str(thread.get("id") or ""))
            if item:
                thread["carry_summary"] = item["carry"]
        tmp = threads_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        tmp.replace(threads_path)
        stats["backup"] = str(backup)
    stats["dry_run"] = bool(dry_run)
    return stats


__all__ = ["apply", "plan", "strip_prefix"]
