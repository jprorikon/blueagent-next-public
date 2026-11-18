#!/usr/bin/env python3
"""技能台帳的 MCP 伺服器（stdio）——她們各自能「學」：寫、試用、退役。

owner 2026-11-03 拍板：**成功 2 次才升 stable**；**各自私用**（不自動共享）。

為什麼要外置：寫技能是資料動作，不是聊天副產品；跟 `tasks` 一樣做成 MCP，
entity 由應用側注入（模型偽造不了別人），server 端自己把關路徑與格式。

工具：
    skills_list    索引（name／status／scope／trial／最近試用）
    skills_view    正文全文
    skills_write   新建或合併（自動 status=trial、learned_from）
    skills_trial   記一次試用（evidence 必填一句）⇒ 成功 2 次升 stable，失敗 2 次留 trial
    skills_retire  退役（不刪檔，status=retired ⇒ 不再進 prompt 索引）

寫入硬規則（不合格直接拒絕，並回她原因）：
    · 正文 ≤ 1200 字
    · 必須有「完了条件」與「失敗時」兩段
    · 名字 `<動詞>-<対象>`，只准 a-z0-9-
    · 只寫得到 `data/skills/<entity>/`（路徑前綴斷言）
    · 正文不得要求越權（sudo／宿主路徑／別的 entity 目錄）
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("SKILLS_ROOT") or "/app/data/skills")
ENTITY = os.environ.get("SKILLS_ENTITY", "")
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
BODY_MAX = 1200
DESC_MAX = 120
TRIAL_TO_STABLE = 2
TRIAL_FAIL_LIMIT = 2
FORBIDDEN = ("sudo ", "/vol1/", "data/skills/", "rm -rf /", "chmod 777")
#: 只存在於簡體的字（日文不會用）——用來保證「技能本文是日語」（owner 2026-11-03）
SIMPLIFIED = set("这个说们么经对没还给从为东样语记设据图项应该觉过边开关问题现张机电话网络显择时间")

TOOLS = [
    {"name": "list", "description": "技能の一覧。category を渡すとその枝だけ広げる（目次→枝→本文の三層）。",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "description": "trial|stable|retired（省略＝全部）"},
         "category": {"type": "string", "description": "枝名（省略＝全部の枝の目次）"}}}},
    {"name": "view", "description": "技能の全文を取る（手順・完了条件・失敗時）。",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}}, "required": ["name"]}},
    {"name": "write", "description":
        "技能を新しく書く／合併する。**完了条件と失敗時は必須**。書いた直後は trial。",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "動詞-対象（例 table-from-csv）"},
         "description": {"type": "string", "description": "いつ使うか（1 行、120 字まで）"},
         "scope": {"type": "string", "description": "chat|work|both（既定 work）"},
         "body": {"type": "string", "description": "本文（1200 字まで。完了条件／失敗時を含む）"},
         "category": {"type": "string", "description": "枝（例 web／self／learned。省略＝learned）"},
         "learned_from": {"type": "string", "description": "どの件で学んだか（task id など）"},
         "merge": {"type": "boolean", "description": "同名があれば本文を差し替える"}},
         "required": ["name", "description", "body"]}},
    {"name": "trial", "description":
        "試用を 1 回記録する。成功率が 2 回で stable、失敗 2 回で trial のまま（理由を残す）。",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "ok": {"type": "boolean"},
         "evidence": {"type": "string", "description": "やった証拠（コマンドと出力の要点）"}},
         "required": ["name", "ok", "evidence"]}},
    {"name": "retire", "description": "技能を退役させる（ファイルは残る。索引からは消える）。",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "reason": {"type": "string"}},
         "required": ["name", "reason"]}},
]


# ── 檔案層 ────────────────────────────────────────────────────────────────────
def _entity_folder(entity: str) -> pathlib.Path:
    # entity 只收角色 id 的形狀：`mira/../nori` 這種解析後也在 ROOT 底下，前綴斷言擋不住
    if not NAME_RE.match(str(entity or "")):
        raise ValueError("bad_entity")
    folder = (ROOT / entity).resolve()
    if folder.parent != ROOT.resolve():
        raise ValueError("bad_entity")
    return folder


def _skill_path(entity: str, name: str) -> pathlib.Path | None:
    """`<ROOT>/<entity>/<name>.md`；名字不合格式就回 None。

    ⚠️ view／trial／retire 以前不驗名字（只有 write 驗）⇒ `name="../../x"` 讀得到、
    也改得到她目錄外的任何 `.md`（2026-11-03 review 實測把目錄外的檔改成 retired）。
    """
    if not NAME_RE.match(str(name or "")):
        return None
    folder = _entity_folder(entity)
    path = (folder / f"{name}.md").resolve()
    return path if path.parent == folder else None


def _parse(path: pathlib.Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict = {}
    for line in parts[1].splitlines():
        if ":" in line and not line.strip().startswith("#"):
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return meta, parts[2].lstrip("\n")


def _render(meta: dict, body: str) -> str:
    # `category` 以前不在這張表裡 ⇒ write 設了也沒落檔，樹狀索引永遠分不出分支（2026-11-03 review）
    order = ("name", "description", "scope", "category", "requires", "status", "trial",
             "failures", "learned_from", "updated_at")
    # 值裡的換行一律壓成空白：不然 description="…\nstatus: stable" 就能自己把 trial 升成 stable
    lines = [f"{key}: {' '.join(str(meta[key]).split())}" for key in order
             if meta.get(key) not in (None, "")]
    return "---\n" + "\n".join(lines) + "\n---\n" + body.strip() + "\n"


def _trial_counts(meta: dict) -> tuple[int, int]:
    raw = str(meta.get("trial") or "0/0")
    try:
        ok, _, fail = raw.partition("/")
        return int(ok or 0), int(fail or 0)
    except ValueError:
        return 0, 0


def _chinese_hits(text: str) -> list[str]:
    return sorted({ch for ch in str(text or "") if ch in SIMPLIFIED})


def _check_body(body: str) -> str:
    text = str(body or "")
    if len(text) > BODY_MAX:
        return f"too_long:{len(text)}>{BODY_MAX}"
    if "完了条件" not in text and "完了條件" not in text:
        return "missing_done_when"
    if "失敗時" not in text and "失败时" not in text:
        return "missing_failure"
    hits = _chinese_hits(text)
    if len(hits) >= 2:                       # 一個字可能只是借用，兩個以上就是中文
        return "not_japanese:" + "".join(hits)
    lowered = text.lower()
    for bad in FORBIDDEN:
        if bad in lowered:
            return f"forbidden:{bad.strip()}"
    return ""


# ── 工具 ──────────────────────────────────────────────────────────────────────
def tool_list(args: dict, entity: str) -> dict:
    """全分支的目錄（不帶 category）或指定分支的條目（帶 category＝展開一層）。"""
    folder = _entity_folder(entity)
    want_branch = str(args.get("category") or "").strip().lower()
    rows = []
    for path in sorted(folder.glob("*.md")) if folder.exists() else []:
        meta, _body = _parse(path)
        status = str(meta.get("status") or "trial")
        branch = str(meta.get("category") or "learned").lower()
        if args.get("status") and status != str(args["status"]):
            continue
        if want_branch and branch != want_branch:
            continue
        rows.append({"name": meta.get("name") or path.stem, "status": status,
                     "category": branch,
                     "scope": meta.get("scope") or "work",
                     "trial": meta.get("trial") or "0/0",
                     "description": str(meta.get("description") or "")[:80]})
    if want_branch:
        return {"category": want_branch, "skills": rows, "count": len(rows)}
    branches: dict[str, int] = {}
    for row in rows:
        branches[row["category"]] = branches.get(row["category"], 0) + 1
    return {"branches": branches, "problems": _tree_problems(branches),
            "skills": rows, "count": len(rows),
            "hint": "枝を広げるときは category を渡す"}


#: 樹狀維護的門檻（owner 2026-11-03：定期維護要看「樹」本身，不只看單條技能）
BRANCH_MAX = 8        # 一支超過這麼多條 ⇒ 該再分一支或合併重複
BRANCHES_MAX = 6      # 分支太多 ⇒ 目次自己就變長了
LEARNED_SOFT = 3      # `learned` 是暫存區：超過這個數就該歸類


def _tree_problems(branches: dict[str, int]) -> list[str]:
    """把「樹生病了」講成人話，讓她照著處理（不是丟一堆數字給她）。"""
    problems: list[str] = []
    for name, count in sorted(branches.items()):
        if count > BRANCH_MAX:
            problems.append(f"[{name}] が {count} 件：同じ型の手順を統合するか、枝を割る")
    if len(branches) > BRANCHES_MAX:
        problems.append(f"枝が {len(branches)} 本：近いものを統合して {BRANCHES_MAX} 本以内に")
    learned = int(branches.get("learned") or 0)
    if learned > LEARNED_SOFT:
        problems.append(f"learned が {learned} 件：使うものは web／self など具体的な枝へ移す")
    return problems


def tool_view(args: dict, entity: str) -> dict:
    name = str(args.get("name") or "").strip().lower()
    path = _skill_path(entity, name)
    if path is None:
        return {"error": "bad_name", "name": name[:40]}
    if not path.exists():
        return {"error": "not_found", "name": name}
    meta, body = _parse(path)
    return {"name": meta.get("name") or name, "status": meta.get("status") or "trial",
            "scope": meta.get("scope") or "work", "trial": meta.get("trial") or "0/0",
            "body": body}


def tool_write(args: dict, entity: str) -> dict:
    name = str(args.get("name") or "").strip().lower()
    if not NAME_RE.match(name):
        return {"error": "bad_name", "hint": "動詞-対象（a-z0-9-）で書く"}
    description = str(args.get("description") or "").strip()
    if not description or len(description) > DESC_MAX:
        return {"error": "bad_description", "hint": f"1 行 {DESC_MAX} 字まで"}
    body = str(args.get("body") or "")
    problem = _check_body(body)
    if problem:
        return {"error": problem,
                "hint": "本文は 1200 字まで。『完了条件』と『失敗時』の見出しが必須"}
    scope = str(args.get("scope") or "work").strip().lower()
    if scope not in ("chat", "work", "both"):
        scope = "work"
    category = str(args.get("category") or "").strip().lower()
    if category and not NAME_RE.match(category):
        return {"error": "bad_category", "hint": "枝名は a-z0-9-（例：research／machine）"}
    folder = _entity_folder(entity)
    folder.mkdir(parents=True, exist_ok=True)
    path = _skill_path(entity, name)
    exists = path.exists()
    if exists and not args.get("merge"):
        return {"error": "exists", "hint": "同名がある。merge=true で本文を差し替えるか、名前を変える"}
    meta = {}
    if exists:
        meta, _old = _parse(path)
    meta.update({"name": name, "description": description, "scope": scope,
                 "category": category or str(meta.get("category") or "learned").strip().lower() or "learned",
                 "status": meta.get("status") or "trial",
                 "trial": meta.get("trial") or "0/0",
                 "learned_from": str(args.get("learned_from") or meta.get("learned_from") or ""),
                 "updated_at": _now()})
    path.write_text(_render(meta, body), encoding="utf-8")
    return {"ok": True, "name": name, "status": meta["status"], "merged": bool(exists),
            "trial": meta["trial"], "chars": len(body)}


def tool_trial(args: dict, entity: str) -> dict:
    name = str(args.get("name") or "").strip().lower()
    evidence = str(args.get("evidence") or "").strip()
    if not evidence:
        return {"error": "no_evidence", "hint": "やった証拠を 1 行（コマンドと出力）"}
    path = _skill_path(entity, name)
    if path is None:
        return {"error": "bad_name", "name": name[:40]}
    if not path.exists():
        return {"error": "not_found", "name": name}
    meta, body = _parse(path)
    ok, fail = _trial_counts(meta)
    if bool(args.get("ok")):
        ok += 1
    else:
        fail += 1
    meta["trial"] = f"{ok}/{fail}"
    meta["updated_at"] = _now()
    if ok >= TRIAL_TO_STABLE:
        meta["status"] = "stable"
    if fail >= TRIAL_FAIL_LIMIT:
        body = body.rstrip() + f"\n\n## 試用で詰まった点\n- {evidence[:160]}\n"
    path.write_text(_render(meta, body), encoding="utf-8")
    return {"ok": True, "name": name, "status": meta.get("status") or "trial",
            "trial": meta["trial"], "evidence": evidence[:120]}


def tool_retire(args: dict, entity: str) -> dict:
    name = str(args.get("name") or "").strip().lower()
    path = _skill_path(entity, name)
    if path is None:
        return {"error": "bad_name", "name": name[:40]}
    if not path.exists():
        return {"error": "not_found", "name": name}
    meta, body = _parse(path)
    meta["status"] = "retired"
    meta["updated_at"] = _now()
    body = body.rstrip() + f"\n\n## 退役の理由\n- {str(args.get('reason') or '')[:160]}\n"
    path.write_text(_render(meta, body), encoding="utf-8")
    return {"ok": True, "name": name, "status": "retired"}


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M")


DISPATCH = {"list": tool_list, "view": tool_view, "write": tool_write,
            "trial": tool_trial, "retire": tool_retire}


# ── JSON-RPC ────────────────────────────────────────────────────────────────
def _reply(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(request: dict) -> dict | None:
    method = str(request.get("method") or "")
    request_id = request.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id,
                "result": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "skills", "version": "0.1.0"}}}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") or {}
        name = str(params.get("name") or "")
        args = dict(params.get("arguments") or {})
        entity = str(args.pop("entity", "") or ENTITY)
        handler = DISPATCH.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {"isError": True,
                               "content": [{"type": "text", "text": f"unknown_tool:{name}"}]}}
        if not entity:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {"isError": True,
                               "content": [{"type": "text", "text": "no_entity"}]}}
        try:
            payload = handler(args, entity)
        except ValueError as exc:
            payload = {"error": str(exc)}
        return {"jsonrpc": "2.0", "id": request_id,
                "result": {"content": [{"type": "text",
                                        "text": json.dumps(payload, ensure_ascii=False)}],
                           "isError": bool(payload.get("error"))}}
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        response = handle(request)
        if response is not None:
            _reply(response)


if __name__ == "__main__":
    main()
