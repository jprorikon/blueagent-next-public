#!/usr/bin/env python3
"""三人房的六个指标（GROUP-SELF-CHAT §10，M5d）：群聊「像不像人在聊」用分布判，不用形容词。

数据来源：
  · 消息：App Server 的群聊历史（真值）。`--messages export.json`（`{"messages":[…]}` 或列表）
    或直接连线：`--live`（用 `BLUEAGENT_APP_SERVER_URL`／`BLUEAGENT_APP_SERVER_TOKENS` 里任一角色的 token 读）。
  · 已读：`data/app-adapter/group-ledger.json` 里的拿起决定（「看到了」才算已读）。没有就跳过第 5 项。

纪律（§10）：只算**这一版程序**的数据——用 `--since`（部署时刻，epoch 秒或 `YYYY-MM-DD HH:MM`）切掉旧数据。
每条判据都标出它是「量到的」还是「先验」。

    python3 agent/tools/group_stats.py --live --since "2026-11-08 07:00"
    python3 agent/tools/group_stats.py --messages room.json --ledger data/app-adapter/group-ledger.json
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import time
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 同一人连续几个泡泡算一次发言（一轮 1–3 泡泡）；超过这么久就算新的一次
SAME_TURN_S = 20.0
#: owner 一句之后多久内有角色开口才算「有人回」
REPLY_WINDOW_S = 600.0
#: 相邻间隔太长就是两段对话，不进中位数
GAP_CAP_S = 1800.0

#: 判据：(说明, 来源)。来源＝「先验」(aster 的先验值) / 「量到的」(aster 实测语料)
CRITERIA = {
    "zero_reply": ("owner 一句 → 0 人回的比例 > 20%", "量到的"),
    "rotation_abc": ("三人轮流 A→B→C 的比例 < 0.5", "量到的"),
    "pingpong_aba": ("A→B→A 紧邻 ≤ 0.3", "量到的"),
    "gap_median": ("相邻发言间隔中位 7–15 秒", "量到的"),
    "read_no_reply": ("已读不回比例 > 0", "先验"),
    "short": ("短句众数落在 6–10 字", "量到的"),
}


def _text(message: dict) -> str:
    return " ".join(str(part.get("text") or "").strip() for part in message.get("parts") or []
                    if isinstance(part, dict) and str(part.get("text") or "").strip())


def _author(message: dict) -> tuple[str, str]:
    author = message.get("author") or {}
    return str(author.get("id") or message.get("author_id") or ""), str(author.get("kind") or "")


def normalize(rows, since: float = 0.0) -> list[dict]:
    """排序、切掉 `since` 之前的；每则留 `at／who／kind／text`。"""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        at = float(row.get("created_at") or 0.0)
        if at < since:
            continue
        who, kind = _author(row)
        out.append({"id": str(row.get("id") or ""), "at": at, "who": who, "kind": kind,
                    "text": _text(row)})
    return sorted(out, key=lambda item: (item["at"], item["id"]))


def utterances(messages: list[dict]) -> list[dict]:
    """同一人 `SAME_TURN_S` 内的连续泡泡并成一次发言（轮流感、间隔按「发言」算，不按泡泡）。"""
    turns: list[dict] = []
    for row in messages:
        last = turns[-1] if turns else None
        if last and last["who"] == row["who"] and row["at"] - last["end"] <= SAME_TURN_S:
            last["end"] = row["at"]
            continue
        turns.append({"who": row["who"], "kind": row["kind"], "at": row["at"], "end": row["at"]})
    return turns


def zero_reply(turns: list[dict]) -> tuple[float | None, int]:
    owners = [index for index, turn in enumerate(turns) if turn["kind"] == "owner"]
    if not owners:
        return None, 0
    silent = 0
    for index in owners:
        start = turns[index]["end"]
        answered = False
        for turn in turns[index + 1:]:
            if turn["kind"] == "owner" or turn["at"] - start > REPLY_WINDOW_S:
                break
            if turn["kind"] == "entity":
                answered = True
                break
        silent += 0 if answered else 1
    return silent / len(owners), len(owners)


def triples(turns: list[dict]) -> tuple[float | None, float | None, int]:
    """只看角色之间的连续三次发言：A→B→C（三人都不同）与 A→B→A。"""
    ai = [turn["who"] for turn in turns if turn["kind"] == "entity"]
    windows = [ai[i:i + 3] for i in range(len(ai) - 2)]
    if not windows:
        return None, None, 0
    abc = sum(1 for a, b, c in windows if len({a, b, c}) == 3)
    aba = sum(1 for a, b, c in windows if a == c and a != b)
    return abc / len(windows), aba / len(windows), len(windows)


def gap_median(turns: list[dict]) -> tuple[float | None, int]:
    gaps = [later["at"] - earlier["end"] for earlier, later in zip(turns, turns[1:])
            if 0.0 <= later["at"] - earlier["end"] <= GAP_CAP_S]
    return (statistics.median(gaps), len(gaps)) if gaps else (None, 0)


def read_no_reply(messages: list[dict], ledger: dict | None, conversation_id: str = "") -> tuple[float | None, int]:
    """账本里「看到了」的（角色, 消息）里，之后 `REPLY_WINDOW_S` 内她没开口的比例。"""
    if not ledger:
        return None, 0
    by_id = {row["id"]: row for row in messages}
    seen = []
    for key, value in (ledger.get("decisions") or {}).items():
        parts = str(key).split("\0")
        if len(parts) != 3 or not value or not value[0]:
            continue
        room, message_id, entity = parts
        if conversation_id and room != conversation_id:
            continue
        if message_id in by_id:
            seen.append((by_id[message_id], entity))
    if not seen:
        return None, 0
    quiet = 0
    for message, entity in seen:
        spoke = any(row["who"] == entity and 0.0 < row["at"] - message["at"] <= REPLY_WINDOW_S
                    for row in messages)
        quiet += 0 if spoke else 1
    return quiet / len(seen), len(seen)


def short_lines(messages: list[dict]) -> dict:
    lengths = [len(row["text"]) for row in messages if row["kind"] == "entity" and row["text"]]
    if not lengths:
        return {"count": 0}
    buckets = Counter(_bucket(length) for length in lengths)
    return {"count": len(lengths),
            "le5": sum(1 for n in lengths if n <= 5) / len(lengths),
            "le10": sum(1 for n in lengths if n <= 10) / len(lengths),
            "mode_bucket": buckets.most_common(1)[0][0],
            "buckets": dict(sorted(buckets.items(), key=lambda item: _bucket_order(item[0])))}


_BUCKETS = ((5, "1-5"), (10, "6-10"), (20, "11-20"), (40, "21-40"))


def _bucket(length: int) -> str:
    for limit, name in _BUCKETS:
        if length <= limit:
            return name
    return "41+"


def _bucket_order(name: str) -> int:
    names = [label for _limit, label in _BUCKETS] + ["41+"]
    return names.index(name)


def compute(rows, ledger: dict | None = None, *, since: float = 0.0, conversation_id: str = "") -> dict:
    messages = normalize(rows, since)
    turns = utterances(messages)
    zero, owners = zero_reply(turns)
    abc, aba, windows = triples(turns)
    gap, gaps = gap_median(turns)
    quiet, seen = read_no_reply(messages, ledger, conversation_id)
    short = short_lines(messages)
    verdict = {
        "zero_reply": None if zero is None else zero > 0.20,
        "rotation_abc": None if abc is None else abc < 0.5,
        "pingpong_aba": None if aba is None else aba <= 0.3,
        "gap_median": None if gap is None else 7.0 <= gap <= 15.0,
        "read_no_reply": None if quiet is None else quiet > 0.0,
        "short": None if not short.get("count") else short["mode_bucket"] == "6-10",
    }
    return {"messages": len(messages), "turns": len(turns),
            "zero_reply": zero, "owner_turns": owners,
            "rotation_abc": abc, "pingpong_aba": aba, "ai_windows": windows,
            "gap_median_s": gap, "gaps": gaps,
            "read_no_reply": quiet, "seen": seen,
            "short": short, "verdict": verdict}


def _fmt(value, digits: int = 2) -> str:
    return "—" if value is None else (f"{value:.{digits}f}" if isinstance(value, float) else str(value))


def report(stats: dict) -> str:
    v = stats["verdict"]
    mark = {True: "PASS", False: "FAIL", None: "N/A "}
    short = stats["short"]
    lines = [f"消息 {stats['messages']} 则／发言 {stats['turns']} 次",
             f"[{mark[v['zero_reply']]}] owner 一句 → 0 人回：{_fmt(stats['zero_reply'])}"
             f"（owner 发言 {stats['owner_turns']} 次）  判据：{CRITERIA['zero_reply'][0]}（{CRITERIA['zero_reply'][1]}）",
             f"[{mark[v['rotation_abc']]}] rotation_abc：{_fmt(stats['rotation_abc'])}"
             f"（角色三连 {stats['ai_windows']} 组）  判据：{CRITERIA['rotation_abc'][0]}（{CRITERIA['rotation_abc'][1]}）",
             f"[{mark[v['pingpong_aba']]}] pingpong_aba：{_fmt(stats['pingpong_aba'])}"
             f"  判据：{CRITERIA['pingpong_aba'][0]}（{CRITERIA['pingpong_aba'][1]}）",
             f"[{mark[v['gap_median']]}] 相邻间隔中位：{_fmt(stats['gap_median_s'], 1)} 秒"
             f"（{stats['gaps']} 个间隔）  判据：{CRITERIA['gap_median'][0]}（{CRITERIA['gap_median'][1]}）",
             f"[{mark[v['read_no_reply']]}] 已读不回：{_fmt(stats['read_no_reply'])}"
             f"（看到 {stats['seen']} 次）  判据：{CRITERIA['read_no_reply'][0]}（{CRITERIA['read_no_reply'][1]}）",
             f"[{mark[v['short']]}] 短句：≤5 字 {_fmt(short.get('le5'))}／≤10 字 {_fmt(short.get('le10'))}"
             f"，众数 {short.get('mode_bucket', '—')}，分布 {short.get('buckets', {})}"
             f"  判据：{CRITERIA['short'][0]}（{CRITERIA['short'][1]}）"]
    return "\n".join(lines)


def _since(raw: str) -> float:
    raw = str(raw or "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M"))


def fetch_live(env=None, *, pages: int = 20) -> tuple[str, list[dict]]:
    """用任一角色的 token 读固定房的全部（最多 `pages`×200 则）消息。"""
    from agent.adapter.app_read_tools import _tokens
    from agent.adapter.app_server import AppServerClient
    env = os.environ if env is None else env
    base = str(env.get("BLUEAGENT_APP_SERVER_URL") or "").strip()
    tokens = _tokens(str(env.get("BLUEAGENT_APP_SERVER_TOKENS") or ""))
    if not base or not tokens:
        raise SystemExit("需要 BLUEAGENT_APP_SERVER_URL 与 BLUEAGENT_APP_SERVER_TOKENS")
    client = AppServerClient(base, next(iter(tokens.values())))
    rooms = client.board_call("chat.conversations", {})
    rooms = rooms if isinstance(rooms, list) else (rooms or {}).get("conversations") or []
    room = next((row["id"] for row in rooms if isinstance(row, dict) and row.get("kind") == "group"), "")
    if not room:
        raise SystemExit("找不到固定房")
    rows, before = [], ""
    for _ in range(pages):
        query = {"conversation_id": room, "limit": 200}
        if before:
            query["before"] = before
        page = client.board_call("chat.messages", query)
        rows = list(page.get("messages") or []) + rows
        before = str(page.get("next_before") or "")
        if not before:
            break
    return room, rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--messages", help="导出的消息 JSON（{\"messages\":[…]} 或列表）")
    parser.add_argument("--live", action="store_true", help="直接从 App Server 读固定房")
    parser.add_argument("--ledger", default=str(ROOT / "data" / "app-adapter" / "group-ledger.json"))
    parser.add_argument("--since", default="", help="只算这之后（部署时刻）：epoch 秒或 'YYYY-MM-DD HH:MM'")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是报表")
    args = parser.parse_args(argv)
    room = ""
    if args.live:
        room, rows = fetch_live()
    elif args.messages:
        data = json.loads(pathlib.Path(args.messages).read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("messages") or []
    else:
        parser.error("给 --messages 或 --live")
    ledger_path = pathlib.Path(args.ledger)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else None
    stats = compute(rows, ledger, since=_since(args.since), conversation_id=room)
    print(json.dumps(stats, ensure_ascii=False, indent=2) if args.json else report(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
