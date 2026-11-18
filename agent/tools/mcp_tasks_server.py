#!/usr/bin/env python3
"""任務台帳的 MCP 伺服器（stdio）——把「看板＋日曆」做成她可以自己操作的外部能力。

為什麼要外置（owner 2026-11-03「這套狀態器可以做成一個外置的 MCP」）：
她需要**自己**插任務、查任務、提交狀態；而這些狀態不是聊天的副產品，
是一個獨立的小系統（`data/tasks/<entity>.json`）。做成 MCP ⇒ 換模型、換殼、別的 agent
都能用同一份台帳，不用改她的程式。

工具（客戶端會加前綴 `mcp_tasks_…`）：

    board     看板：一列一個狀態（pending／processing／hanging／locked／outdated／completed）
    calendar  日曆：未來 N 天各排了什麼；沒定日子的列在 unscheduled
    add       插一條（可帶 when＝今天／明天／YYYY-MM-DD、note、priority）
    state     提交狀態（locked 可帶 wait_minutes，時間到自動回到 pending）
    note      只改備註

環境變數：
    TASKS_ROOT    台帳目錄（預設 ../../data/tasks，相對於本檔）
    TASKS_ENTITY  這一台 server 預設服務哪個角色（應用側會用 `inject` 覆蓋成當前角色）
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.brain.runtime.inner.task_ledger import STATES, TaskLedger  # noqa: E402

TASKS_ROOT = pathlib.Path(os.environ.get("TASKS_ROOT") or (ROOT / "data" / "tasks"))
DEFAULT_ENTITY = str(os.environ.get("TASKS_ENTITY") or "mira")

_SCHEMA_ENTITY = {"type": "string",
                  "description": "どの役の台帳か（アプリ側が自動で入れる。通常は指定しない）"}

TOOLS = [
    {"name": "board",
     "description": ("自分のタスク看板。まずこれを開いて「今なにが残っているか」を確かめる。"
                     "列＝pending（これから）／processing（進行中）／hanging（中断・続き待ち）／"
                     "locked（待ち）／outdated（古い）／completed（済み）。"),
     "inputSchema": {"type": "object", "properties": {
         "per_column": {"type": "integer", "description": "各列に何件まで出すか（既定 8）"},
         "entity": _SCHEMA_ENTITY}}},
    {"name": "calendar",
     "description": ("自分の予定表。今日から days 日先までの「その日にやるつもり」を日付ごとに返す。"
                     "日付を決めていないものは unscheduled に入る。毎日の段取りや長期の計画はこれで組む。"),
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "何日先まで（既定 7、最大 90）"},
         "entity": _SCHEMA_ENTITY}}},
    {"name": "add",
     "description": ("タスクを一本足す。思いついたこと・やると決めたことをここに置く"
                     "（どうでもいい雑談は置かない）。when に今日／明日／YYYY-MM-DD を入れると予定表に載る。"),
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "やること（短く）"},
         "when": {"type": "string", "description": "today／tomorrow／YYYY-MM-DD（省略＝日付なし）"},
         "note": {"type": "string", "description": "ひとこと（なぜ／どこまで）"},
         "because": {"type": "string",
                     "description": ("**何がきっかけで**足すのか（オーナーの一言／外で読んだもの／できあがった作品）。"
                                     "自分のノートを読み返して思いついただけなら足さない")},
         "priority": {"type": "number", "description": "大きいほど先にやる（既定 0）"},
         "entity": _SCHEMA_ENTITY},
         "required": ["title", "because"]}},
    {"name": "state",
     "description": ("タスクの状態を提出する。pending／processing／hanging／locked／outdated／completed。"
                     "locked のときは wait_minutes（何分後に見直すか）を付ける。"),
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "board に出ている id"},
         "state": {"type": "string", "enum": list(STATES)},
         "note": {"type": "string", "description": "理由や待ち内容をひとこと"},
         "wait_minutes": {"type": "integer", "description": "locked のときの再確認までの分"},
         "entity": _SCHEMA_ENTITY},
         "required": ["id", "state"]}},
    {"name": "note",
     "description": "タスクのメモだけ更新する（状態は変えない）。",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "note": {"type": "string"},
         "entity": _SCHEMA_ENTITY},
         "required": ["id", "note"]}},
    {"name": "ack",
     "description": ("主人からの批注を「見た」ことにする（処理が済んだら呼ぶ）。"
                     "id を省くと全部。**状態は変えない**——どうするかは自分で決めて `state` で提出する。"),
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "批注が付いているタスク id（省略＝全部）"},
         "entity": _SCHEMA_ENTITY}}},
]


#: 她自己一天最多加幾件（防自旋 ⑥）
SELF_ADD_PER_DAY = int(os.environ.get("BLUEAGENT_TASKS_SELF_ADD_PER_DAY") or 2)


def _ledger(entity: str) -> TaskLedger:
    return TaskLedger(TASKS_ROOT, str(entity or DEFAULT_ENTITY))


def _when(value: str, now: float) -> float:
    """`today`／`tomorrow`／`YYYY-MM-DD` → 那天早上 9 點的時間戳（沒有就 0）。"""
    text = str(value or "").strip().lower()
    if not text:
        return 0.0
    day = ""
    if text in ("today", "今日", "きょう", "kyou"):
        day = time.strftime("%Y-%m-%d", time.localtime(now))
    elif text in ("tomorrow", "明日", "あした", "ashita"):
        day = time.strftime("%Y-%m-%d", time.localtime(now + 86400))
    else:
        try:
            time.strptime(text, "%Y-%m-%d")
            day = text
        except ValueError:
            return 0.0
    return time.mktime(time.strptime(f"{day} 09:00", "%Y-%m-%d %H:%M"))


def _ok(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _fail(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": str(message)}]}


def _call(name: str, args: dict) -> dict:
    entity = str(args.get("entity") or DEFAULT_ENTITY)
    # entity 直接拼成檔名 ⇒ 只收角色 id 的形狀（擋 `../` 跳出 TASKS_ROOT）
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", entity):
        return _fail(f"bad entity: {entity[:40]}")
    ledger = _ledger(entity)
    now = time.time()
    if name == "board":
        per = int(args.get("per_column") or 8)
        return _ok({"entity": entity, **ledger.board(limit_per_column=max(1, min(40, per)))})
    if name == "calendar":
        days = int(args.get("days") or 7)
        return _ok({"entity": entity, **ledger.calendar(days=max(1, min(90, days)), now=now)})
    if name == "add":
        title = str(args.get("title") or "").strip()
        if not title:
            return _fail("title が空")
        # 防自旋 ⑥（owner 2026-11-09）：自分で足すタスクは「きっかけ」必須・一日 2 件まで。
        # 自分の産出から自分の仕事を生み続けると、台帳が輪換より先に回り続ける。
        because = str(args.get("because") or "").strip()
        if not because:
            return _fail("because が空（何がきっかけで足すのかを書く。思いつきだけなら足さない）")
        if ledger.self_added_today(now=now) >= SELF_ADD_PER_DAY:
            return _fail(f"今日はもう {SELF_ADD_PER_DAY} 件足した（明日にするか、今あるものを片づける）")
        item = ledger.add(title, note=str(args.get("note") or ""),
                          priority=float(args.get("priority") or 0),
                          due_at=_when(str(args.get("when") or ""), now),
                          origin="self", because=because)
        return _ok({"task": item})
    if name == "state":
        task_id = str(args.get("id") or "").strip()
        if not task_id:
            return _fail("id が空")
        if ledger.get(task_id) is None:
            return _fail(f"id が見つからない: {task_id}")
        wait = args.get("wait_minutes")
        until = 0.0
        try:
            if wait not in (None, "", 0, "0"):
                until = now + float(wait) * 60.0
        except (TypeError, ValueError):
            until = 0.0
        return _ok({"task": ledger.set_state(task_id, str(args.get("state") or ""),
                                             note=str(args.get("note") or ""),
                                             wait_until=until)})
    if name == "note":
        task_id = str(args.get("id") or "").strip()
        if not task_id or ledger.get(task_id) is None:
            return _fail(f"id が見つからない: {task_id}")
        return _ok({"task": ledger.patch(task_id, note=str(args.get("note") or "")[:200])})
    if name == "ack":
        marked = ledger.ack_comments(str(args.get("id") or ""))
        return _ok({"acknowledged": marked, "unseen": len(ledger.unseen_comments())})
    return _fail(f"unknown tool: {name}")


def handle(message: dict) -> dict | None:
    method = str(message.get("method") or "")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "tasks", "version": "1.0"}}}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        try:
            payload = _call(name, dict(params.get("arguments") or {}))
        except Exception as exc:                          # noqa: BLE001 - 失敗要回錯誤，不能讓 server 死
            payload = _fail(f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": payload}
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = handle(message)
        if reply is None:
            continue
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
