#!/usr/bin/env python3
"""產生 `docs/FILE-TREE.md` 的檔案樹（每個 Python 模組帶它 docstring 的第一行）。

用法：
  python3 agent/tools/gen_file_tree.py          # 改寫 docs/FILE-TREE.md 標記之間的那一段
  python3 agent/tools/gen_file_tree.py --check  # 只檢查：樹裡列的路徑與倉庫實際的對不對得上（exit 1＝要重產）

為什麼用產生的：手寫的目錄說明幾天就過期（2026-10-28 清掉的交接檔裡，路徑一半已經不存在）。
說明文字取自各檔自己的 docstring／`/// <summary>`——要改說明就改那個檔，不要改這份產物。
`--check` 只比「有哪些路徑」，不比說明文字：改 docstring 不必重產，新增／刪除／搬檔才要。
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "FILE-TREE.md"
BEGIN = "<!-- tree:begin（agent/tools/gen_file_tree.py 產生，不要手改） -->"
END = "<!-- tree:end -->"
#: 逐檔列出的 Python 目錄（其餘目錄只列摘要）；根目錄只有 agent／app／mr 三個本體（2026-11-09）
AGENT_TREES = ("agent/brain", "agent/adapter", "agent/server", "agent/tools")
PY_TREES = ("app",) + AGENT_TREES + ("mr",)
AGENT_NOTES = {"agent/brain": "主控與記憶核心（依賴只准由外往內，見 test_package_layering）",
               "agent/adapter": "外部渠道 canonical 實作；無 blueagent_next 兼容命名空間",
               "agent/server": "Agent 自己的 HTTP／SSE（/api、/admin）；App Server 是 app/server",
               "agent/tools": "維運／遷移／診斷腳本（不被生產碼 import）"}
NEXT_SCRIPTS = "app/client/BlueAgentUnity/Assets/Scripts/Next"
MR_SCRIPTS = "mr/BlueAgentUnity/Assets/Scripts/MR"
LOGIC = "app/client/shared/BlueAgent.Logic"
WIDTH = 64


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    # `git ls-files` 仍會列出「已刪除但還沒 git add」的路徑；產生文件樹時不能再去讀它。
    return [line for line in out.stdout.splitlines() if line and (ROOT / line).is_file()]


def _clip(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= WIDTH else text[:WIDTH - 1] + "…"


def py_summary(path: pathlib.Path) -> str:
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    except (SyntaxError, UnicodeDecodeError, OSError):
        return ""
    first = next((line.strip() for line in doc.splitlines() if line.strip()), "")
    return _clip(first)


def cs_summary(path: pathlib.Path) -> str:
    """第一個 `/// <summary>` 之後的第一行文字。"""
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for index, line in enumerate(lines):
        if "<summary>" not in line:
            continue
        inline = re.sub(r".*<summary>", "", line).replace("</summary>", "").strip()
        if inline:
            return _clip(inline)
        for follow in lines[index + 1:index + 4]:
            text = follow.strip().lstrip("/").strip()
            if text and not text.startswith("<"):
                return _clip(text.replace("</summary>", ""))
    return ""


def md_summary(path: pathlib.Path) -> str:
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("#"):
            return _clip(line.lstrip("#").strip())
    return ""


def _row(indent: int, name: str, note: str) -> str:
    left = "  " * indent + name
    return f"{left:<44} # {note}" if note else left


def py_tree(files: list[str], top: str, base: int = 1) -> list[str]:
    """一個 Python 目錄：子套件（`__init__` 的 docstring）＋每個模組一行。`top` 可帶斜線（`agent/brain`）。"""
    rows: list[str] = []
    mods = sorted(f for f in files if f.startswith(top + "/") and f.endswith(".py")
                  and not f.startswith("app/tests/"))
    seen_dirs: set[str] = set()
    lift = top.count("/")
    for rel in mods:
        parts = rel.split("/")
        for depth in range(lift + 1, len(parts) - 1):
            folder = "/".join(parts[:depth + 1])
            if folder in seen_dirs:
                continue
            seen_dirs.add(folder)
            init = ROOT / folder / "__init__.py"
            rows.append(_row(base + depth - lift, parts[depth] + "/", py_summary(init) if init.exists() else ""))
        if parts[-1] == "__init__.py":
            continue
        rows.append(_row(base + len(parts) - 1 - lift, parts[-1], py_summary(ROOT / rel)))
    return rows


def expected_paths(files: list[str]) -> set[str]:
    """樹裡必須逐條列出的路徑（`--check` 的判據）。"""
    want = {f for f in files if f.endswith(".py") and f.startswith(tuple(t + "/" for t in PY_TREES))
            and not f.endswith("__init__.py") and not f.startswith("app/tests/")}
    want |= {f for f in files if f.startswith(NEXT_SCRIPTS + "/") and f.endswith(".cs")}
    want |= {f for f in files if f.startswith(MR_SCRIPTS + "/") and f.endswith(".cs")}
    want |= {f for f in files if f.startswith("docs/") and f.endswith(".md")}
    want |= {f for f in files if f.startswith("app/client/") and f.count("/") == 2
             and f.endswith(".md")}
    want |= {f for f in files if f == "mr/README.md"}
    want |= {f for f in files if "/" not in f}
    want |= {f for f in files if f.count("/") == 1 and f.startswith(("agent/", "app/"))
             and not f.endswith("__init__.py")}
    want |= {f for f in files if f.startswith(("agent/tests/", "app/tests/")) and f.endswith(".py")
             and "/test_" not in f}
    return want


def client_tree(files: list[str]) -> list[str]:
    """Unity 手機工程與共用 C#；Quest 專有工程在頂層 mr/。"""
    client = [f for f in files if f.startswith("app/client/")]
    logic = [f for f in client if f.startswith(LOGIC + "/") and f.endswith(".cs")]
    logic_tests = [f for f in client if f.startswith("app/client/shared/BlueAgent.Logic.Tests/")
                   and f.endswith(".cs")]
    scripts = [f for f in client if f.startswith("app/client/BlueAgentUnity/Assets/Scripts/")
               and f.endswith(".cs")]
    next_cs = sorted(f for f in scripts if f.startswith(NEXT_SCRIPTS + "/"))
    rows: list[str] = []
    for doc in sorted(f for f in client if f.count("/") == 2 and f.endswith(".md")):
        rows.append(_row(3, doc.rsplit("/", 1)[1], md_summary(ROOT / doc)))
    rows.append(_row(3, "shared/BlueAgent.Logic/", f"共用邏輯層 C#（{len(logic)} 檔，不碰平台 API）"))
    rows.append(_row(3, "shared/BlueAgent.Logic.Tests/", f"NUnit 對拍測試（{len(logic_tests)} 檔）"))
    rows.append(_row(3, "fixtures/", f"上游產生的真值 JSON（{sum(1 for f in client if f.startswith('app/client/fixtures/'))} 檔）"))
    rows.append(_row(3, "tools/", f"建置／部署／對拍腳本（{sum(1 for f in client if f.startswith('app/client/tools/'))} 檔）"))
    rows.append(_row(3, "BlueAgentUnity/Assets/Scripts/", f"手機與共享執行期 C#（{len(scripts)} 檔）"))
    rows.append(_row(4, "Next/", "blueagent-next 的 master／guest 端（Next.unity）"))
    for rel in next_cs:
        rows.append(_row(5, rel.rsplit("/", 1)[1], cs_summary(ROOT / rel)))
    return rows


def mr_tree(files: list[str]) -> list[str]:
    """Quest 專有工程；共享 Assets 只在構建工作區組合，不複製進倉庫。"""
    rows = [_row(2, "README.md", md_summary(ROOT / "mr/README.md")),
            _row(2, "BlueAgentUnity/Assets/Scripts/", "Quest 專有執行期"),
            _row(3, "MR/", "OpenXR／空間／手勢／通話（MR.unity）")]
    for rel in sorted(f for f in files if f.startswith(MR_SCRIPTS + "/") and f.endswith(".cs")):
        rows.append(_row(4, rel.rsplit("/", 1)[1], cs_summary(ROOT / rel)))
    return rows


def _tests(files: list[str], folder: str, indent: int, note: str) -> list[str]:
    tests = [f for f in files if f.startswith(folder + "/") and f.endswith(".py")]
    rows = [_row(indent, "tests/", f"{note} {sum(1 for f in tests if '/test_' in f)} 支")]
    for helper in sorted(f for f in tests if "/test_" not in f):
        rows.append(_row(indent + 1, helper.rsplit("/", 1)[1], py_summary(ROOT / helper)))
    return rows


def _top_files(files: list[str], folder: str, indent: int) -> list[str]:
    rows = []
    for name in sorted(f for f in files if f.startswith(folder + "/") and f.count("/") == 1
                       and not f.endswith("__init__.py")):
        rel = name.split("/", 1)[1]
        note = py_summary(ROOT / name) if name.endswith(".py") else ""
        rows.append(_row(indent, rel, note))
    return rows


def agent_tree(files: list[str]) -> list[str]:
    rows = [_row(1, "agent/", "Agent 本體（包名 agent.*）：腦、Agent HTTP、外部渠道、人格、維運腳本")]
    rows += _top_files(files, "agent", 2)
    for top in AGENT_TREES:
        rows.append(_row(2, top.split("/", 1)[1] + "/", AGENT_NOTES[top]))
        rows += py_tree(files, top, base=2)
    for folder, note in (("agent/client", "無構建的 owner 調試台＋管理台（Web，/static、/admin）"),
                         ("agent/persona", "人格檔（SOUL.md、各角色 .md／.interests.md）"),
                         ("agent/motion_service", "GPU 機上的動作生成服務（獨立部署）")):
        count = sum(1 for f in files if f.startswith(folder + "/"))
        rows.append(_row(2, folder.split("/", 1)[1] + "/", f"{note}；{count} 檔"))
    rows += _tests(files, "agent/tests", 2, "離線驗收（run_all.py 連 app/tests 一起跑）")
    return rows


def render(files: list[str]) -> str:
    rows: list[str] = ["```text", "blueagent-next/"]
    # 頂層檔
    for name in sorted(f for f in files if "/" not in f):
        note = md_summary(ROOT / name) if name.endswith(".md") else ""
        rows.append(_row(1, name, note))
    rows += agent_tree(files)
    rows.append(_row(1, "app/", "獨立 App 產品邊界（App Server＋Unity 手機端）；不依賴 Agent"))
    rows += _top_files(files, "app", 2)
    rows += py_tree([f for f in files if f.startswith("app/client/")], "app")
    rows += client_tree(files)
    rows += py_tree([f for f in files if not f.startswith("app/client/")], "app")
    rows += _tests(files, "app/tests", 2, "App 的離線驗收（app/tests/run_all.py 單跑）")
    rows.append(_row(1, "mr/", "Quest 專有工程與構建；共享實作由 app/client 唯一持有"))
    rows += mr_tree(files)
    rows += py_tree(files, "mr")
    rows.append(_row(1, "docs/", "契約與專題設計（現行）"))
    for doc in sorted(f for f in files if f.startswith("docs/") and f.endswith(".md")):
        rows.append(_row(2, doc.split("/", 1)[1], md_summary(ROOT / doc)))
    rows.append("```")
    return "\n".join(rows)


def listed_paths(text: str) -> set[str]:
    """從產物反推它列了哪些路徑（依縮排重建）。"""
    block = text.split(BEGIN, 1)[-1].split(END, 1)[0]
    stack: list[str] = []
    paths: set[str] = set()
    for line in block.splitlines():
        if not line.strip() or line.startswith("```") or line.startswith("blueagent-next/"):
            continue
        name = line.split(" # ", 1)[0].rstrip()
        depth = (len(name) - len(name.lstrip(" "))) // 2 - 1
        name = name.strip()
        stack = stack[:depth] + [name]
        if not name.endswith("/"):
            paths.add("/".join(part.rstrip("/") for part in stack))
    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只檢查路徑是否對得上")
    args = parser.parse_args(argv)
    files = tracked()
    text = DOC.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print(f"✗ {DOC.relative_to(ROOT)} 缺少標記")
        return 1
    if args.check:
        want = expected_paths(files)
        listed = listed_paths(text)
        missing = sorted(want - listed)
        extra = sorted(path for path in listed - want if path.endswith((".py", ".cs", ".md")))
        if missing or extra:
            print("✗ 檔案樹過期了：python3 agent/tools/gen_file_tree.py 重產")
            for path in missing:
                print("   少了 " + path)
            for path in extra:
                print("   多了 " + path)
            return 1
        print(f"✓ 檔案樹與倉庫一致（{len(want)} 條路徑）")
        return 0
    head, rest = text.split(BEGIN, 1)
    _old, tail = rest.split(END, 1)
    DOC.write_text(head + BEGIN + "\n" + render(files) + "\n" + END + tail, encoding="utf-8")
    print(f"✓ 已重產 {DOC.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
