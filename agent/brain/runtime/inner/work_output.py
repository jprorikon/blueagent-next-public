"""作品檔的合併（生活回路的產物）：**一件作品一個檔**，每一步是檔裡的一節。

放在 `runtime/inner/` 的理由：這是「她自己的生活產出」那條線的一部分（同 `task_ledger`）；
而且 `runtime/` 頂層有**只准減少**的檔數棘輪（`test_runtime_top_level_module_count_ratchet`）。

為什麼要有（owner 2026-11-08：「她发过来的不是表，只是一个表的说明」）：
`form="work"` 以前每步各寫一個檔（`{日期}-{活動}-{步驟}.md`），於是「観測表」被拆成三份
筆記——欄位定義一份、記入例一份、使い方一份，**沒有任何一份是那張表**。
主人要的是成品，不是三個半成品。

現在路徑是 `{日期}-{活動}.md`，每一步寫進同一份檔的 `## {步驟}` 節：
同一步重跑就換掉那一節（重做不會堆檔），其他節保留。
"""

from __future__ import annotations

import pathlib


def parse_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """把檔案拆成（標題行, [(節名, 內文)…]）。格式壞掉就當整段是內文。"""
    lines = str(text or "").splitlines()
    title = ""
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("## "):
            current = []
            sections.append((line[3:].strip(), current))
        elif current is not None:
            current.append(line)
    return title, [(name, "\n".join(body).strip("\n")) for name, body in sections]


def render(title: str, sections: list[tuple[str, str]]) -> str:
    parts = [f"# {title}", ""]
    for name, body in sections:
        parts.append(f"## {name}")
        parts.append("")
        parts.append(body.strip("\n"))
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def merge_section(path: pathlib.Path, *, title: str, step: str, body: str) -> pathlib.Path:
    """把 `step` 這一節寫進作品檔（有就換掉，沒有就接在後面），回檔案路徑。"""
    path = pathlib.Path(path)
    old_title, sections = ("", [])
    if path.exists():
        try:
            old_title, sections = parse_sections(path.read_text(encoding="utf-8"))
        except OSError:
            old_title, sections = ("", [])
    kept: list[tuple[str, str]] = []
    replaced = False
    for name, text in sections:
        if name == step:
            kept.append((name, str(body)))
            replaced = True
        else:
            kept.append((name, text))
    if not replaced:
        kept.append((step, str(body)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(old_title or title, kept), encoding="utf-8")
    return path


__all__ = ["merge_section", "parse_sections", "render"]
