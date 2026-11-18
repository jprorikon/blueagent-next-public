#!/usr/bin/env python3
"""換 AEM 核心後的一次性重投影（owner 2026-11-01 升 1,215 行那顆）。

為什麼要這支：新核心的 `project()` 多了 `affect.composition`（valence 按 kind 分解），
而 `state.json` 裡存著**舊版算出來的 view** ⇒ `show()` 的自檢 `project(state) == state["view"]`
一定不成立，會擋成 `STATE_CORRUPT`。那不是檔案壞了，是**版本差一格**。

這支只做一件事：把每個 state 的 `view` 用新核心重算一次並存回。
`episodes`／`receipts`／`policy`／`revision`／`as_of` 一個都不碰。

    python3 agent/tools/aem_reproject.py --data data            # 全部角色
    python3 agent/tools/aem_reproject.py --data data --dry-run  # 只看會動哪些、差異在哪
    python3 agent/tools/aem_reproject.py --data data --entity mira
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agent.brain.vendor import aem_v2  # noqa: E402


def entity_dirs(data: pathlib.Path):
    root = data / "affect"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def reproject(path: pathlib.Path, *, dry_run: bool) -> dict:
    """單一 state.json：重算 view，比對差異，必要時存回。"""
    raw = aem_v2.load_json(path)
    before = raw.get("view") or {}
    view = aem_v2.project(raw, float(raw.get("as_of") or raw.get("revision") or 0.0))
    added = sorted(set(view) - set(before))
    changed = sorted(key for key in set(view) & set(before) if view[key] != before[key])
    result = {"path": str(path), "added": added, "changed": changed,
              "revision": raw.get("revision"), "episodes": len(raw.get("episodes") or [])}
    if dry_run:
        return result
    raw["view"] = view
    store = aem_v2.Store(root=path.parent.parent, agent=path.parent.name)
    store.save(raw)
    result["saved"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="換核心後重投影 AEM 的 view")
    parser.add_argument("--data", default="data", help="BLUEAGENT_DATA_DIR（容器內是 /app/data）")
    parser.add_argument("--entity", default="", help="只做這個角色")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    data = pathlib.Path(args.data)
    targets = entity_dirs(data)
    if args.entity:
        targets = [path for path in targets if path.name == args.entity]
    if not targets:
        print(f"（{data}/affect 底下沒有角色）")
        return 0
    results = []
    for folder in targets:
        state_path = folder / "app" / "state.json"
        if not state_path.exists():
            print(f"  跳過 {folder.name}：沒有 app/state.json")
            continue
        try:
            results.append(reproject(state_path, dry_run=args.dry_run))
        except aem_v2.AEMError as exc:
            print(f"  失敗 {folder.name}：{exc.code}")
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for row in results:
        tags = []
        if row["added"]:
            tags.append("新增 " + ",".join(row["added"]))
        if row["changed"]:
            tags.append("變動 " + ",".join(row["changed"]))
        print(f"{pathlib.Path(row['path']).parts[-3]}: rev {row['revision']}／"
              f"{row['episodes']} 筆 episode／{'; '.join(tags) or '沒有差異'}"
              f"{'（dry-run）' if args.dry_run else '（已存回）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
