#!/usr/bin/env python3
"""產生 `docs/ENVIRONMENT.md`：環境變數／預設值／讀取位置總表。

用法：
  python3 agent/tools/gen_env_table.py          # 重產總表
  python3 agent/tools/gen_env_table.py --check  # 只檢查產物是否與程式一致

掃描 `app/`、`agent/{brain,adapter,server,tools}/`、`mr/` 的 Python 原始碼（不含測試、vendor／構建工作區）。
除了直接的 `os.environ.get(...)`，也辨識注入 mapping、字串常數與動態變數族；
間接讀取的預設值標成「見讀取點」，不猜測執行期資料流。
"""

from __future__ import annotations

import argparse
import ast
import itertools
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "ENVIRONMENT.md"
SOURCE_ROOTS = ("app", "agent/brain", "agent/adapter", "agent/server", "agent/tools", "mr")
ENV_MAPPINGS = {"env", "values", "environ"}
ENV_PREFIXES = (
    "BLUEAGENT_", "DEEPSEEK_", "HERMES_", "TTS_", "DISCORD_", "VISION_",
    "STT_", "HINDSIGHT_", "MEM_", "MCP_", "FIRECRAWL_", "WEATHER_",
    "WHISPER_", "SENSEVOICE_",
)
UPPER_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _sources():
    for folder in SOURCE_ROOTS:
        assert (ROOT / folder).is_dir(), f"{folder} 不見了：搬目錄要同步改 SOURCE_ROOTS"
        for path in sorted((ROOT / folder).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/vendor/" not in rel and not rel.startswith(("mr/.build/", "app/tests/")):
                yield rel, path


def _is_env_mapping(node) -> bool:
    return ((isinstance(node, ast.Attribute) and node.attr == "environ"
             and isinstance(node.value, ast.Name) and node.value.id == "os")
            or (isinstance(node, ast.Name) and node.id in ENV_MAPPINGS))


def _is_env_read(node) -> bool:
    if not isinstance(node, ast.Call) or not node.args:
        return False
    fn = node.func
    return ((isinstance(fn, ast.Attribute) and fn.attr == "get"
             and _is_env_mapping(fn.value))
            or (isinstance(fn, ast.Attribute) and fn.attr == "getenv"
                and isinstance(fn.value, ast.Name) and fn.value.id == "os"))


def _literal(node, constants):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _module_constants(tree) -> dict[str, str]:
    found = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                found[target.id] = node.value.value
    return found


def _loop_values(tree):
    """常數 tuple 迴圈的逐欄值，用來展開角色專屬 env 名。"""
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Tuple) \
                or not isinstance(node.iter, (ast.Tuple, ast.List)):
            continue
        rows = [row.elts for row in node.iter.elts if isinstance(row, (ast.Tuple, ast.List))]
        if not rows or any(len(row) != len(node.target.elts) for row in rows):
            continue
        for index, target in enumerate(node.target.elts):
            if isinstance(target, ast.Name):
                values = [_literal(row[index], {}) for row in rows]
                if all(value is not None for value in values):
                    found[target.id] = values
    return found


def _dynamic_names(node, constants, assignments, loops, depth=0) -> list[str]:
    """把可靜態判定的運算式展開成文字；未知部分保留 `<NAME>`。"""
    if depth > 8:
        return ["<VALUE>"]
    literal = _literal(node, constants)
    if literal is not None:
        return [literal]
    if isinstance(node, ast.Name):
        if node.id in loops:
            return loops[node.id]
        if node.id in assignments:
            return _dynamic_names(assignments[node.id], constants, assignments, loops, depth + 1)
        return [f"<{node.id.upper()}>"]
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append([value.value])
            elif isinstance(value, ast.FormattedValue):
                parts.append(_dynamic_names(value.value, constants, assignments, loops, depth + 1))
        return ["".join(items) for items in itertools.product(*parts)] if parts else []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _dynamic_names(node.left, constants, assignments, loops, depth + 1)
        right = _dynamic_names(node.right, constants, assignments, loops, depth + 1)
        return [a + b for a, b in itertools.product(left, right)]
    if isinstance(node, ast.Call):
        # str(entity)／role.upper() 的動態部分仍保留有意義的佔位名。
        if isinstance(node.func, ast.Name) and node.func.id == "str" and node.args:
            return _dynamic_names(node.args[0], constants, assignments, loops, depth + 1)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"upper", "lower"}:
            return _dynamic_names(node.func.value, constants, assignments, loops, depth + 1)
    return ["<VALUE>"]


def _default(node, parents, constants) -> str:
    call = node
    if len(call.args) > 1:
        value = ast.unparse(call.args[1])
    else:
        parent = parents.get(call)
        if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.Or) \
                and parent.values[0] is call and len(parent.values) == 2:
            value = ast.unparse(parent.values[1])
        else:
            return "—"
    value = " ".join(value.split())
    return value if len(value) <= 56 else value[:53] + "…"


def scan() -> dict[str, dict]:
    rows: dict[str, dict] = {}

    def add(name, default, rel):
        name = str(name or "").strip()
        if not name or name.startswith("<"):
            return
        row = rows.setdefault(name, {"defaults": set(), "files": set()})
        row["defaults"].add(default)
        row["files"].add(rel)

    for rel, path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        constants = _module_constants(tree)
        assignments = {target.id: node.value for node in ast.walk(tree)
                       if isinstance(node, ast.Assign) and len(node.targets) == 1
                       for target in node.targets if isinstance(target, ast.Name)}
        loops = _loop_values(tree)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if _is_env_read(node):
                arg = node.args[0]
                name = _literal(arg, constants)
                if name is not None:
                    add(name, _default(node, parents, constants), rel)
                else:
                    for dynamic in _dynamic_names(arg, constants, assignments, loops):
                        if dynamic.startswith(ENV_PREFIXES):
                            add(dynamic, "見讀取點", rel)
            # 全大寫字串也涵蓋 pick(...)／_env_float(...) 等間接讀取；裸前綴不是變數。
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and UPPER_NAME.fullmatch(node.value) and node.value.startswith(ENV_PREFIXES) \
                    and not node.value.endswith("_"):
                add(node.value, "見讀取點", rel)
    return rows


def render(rows: dict[str, dict]) -> str:
    lines = [
        "# 環境變數總表",
        "",
        "> 由 `python3 agent/tools/gen_env_table.py` 掃描 Python 讀取點產生，請勿手改。",
        "> `tests/test_environment_table.py` 會守著程式與本表一致。",
        ">",
        "> 「—」＝沒有程式碼 fallback（未設時由後續邏輯決定）；「見讀取點」＝經常數、helper",
        "> 或動態名稱間接讀取。`<ROLE>`／`<ENTITY>` 表示依角色／實體展開的變數族。",
        "> 金鑰與 token 只列名稱，不列實際值。",
        "",
        f"共 {len(rows)} 個變數或動態變數族。",
        "",
        "| 變數 | 程式碼預設值 | 讀取位置 |",
        "|---|---|---|",
    ]
    for name in sorted(rows):
        row = rows[name]
        defaults = sorted(row["defaults"], key=lambda value: (value == "見讀取點", value))
        default = "；".join(defaults).replace("|", "\\|")
        files = "<br>".join(f"`{path}`" for path in sorted(row["files"]))
        lines.append(f"| `{name}` | `{default}` | {files} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只檢查，不改寫")
    args = parser.parse_args(argv)
    expected = render(scan())
    if args.check:
        if not DOC.exists() or DOC.read_text(encoding="utf-8") != expected:
            print("✗ 環境變數總表過期：python3 agent/tools/gen_env_table.py 重產")
            return 1
        print("✓ 環境變數總表與程式一致")
        return 0
    DOC.write_text(expected, encoding="utf-8")
    print(f"✓ 已重產 {DOC.relative_to(ROOT)}（{len(scan())} 項）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
