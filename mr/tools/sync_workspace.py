#!/usr/bin/env python3
"""組合 Quest／MR 的 Unity 構建工作區，共享執行期仍只由 app/client 持有。

倉庫中的 ``mr/BlueAgentUnity`` 只放 Quest 專有內容與完整構建設定；手機與 MR 共用的
Assets、Keys、C# 邏輯層和 fixtures 在構建前複製到 ``mr/.build/workspace``。只刷新來源目錄，
保留工作區的 Library／Temp，讓 Unity 可以增量構建。可用 ``BA_MR_BUILD_ROOT`` 把快取放到外置碟。

用法：``python3 mr/tools/sync_workspace.py [--workspace <目錄>]``。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[2]
CLIENT = ROOT / "app" / "client"
MR = ROOT / "mr"
IGNORE = shutil.ignore_patterns(".DS_Store", "._*", "__pycache__", "bin", "obj", ".build")


def replace_tree(source: pathlib.Path, destination: pathlib.Path) -> None:
    if not source.is_dir():
        raise FileNotFoundError(f"缺少構建來源：{source}")
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, ignore=IGNORE)


def sync(workspace: pathlib.Path) -> pathlib.Path:
    """刷新工作區並回傳可直接交給 Unity 的 MR 工程目錄。"""
    workspace = workspace.resolve()
    project = workspace / "mr" / "BlueAgentUnity"
    for relative in ("Assets", "Keys"):
        replace_tree(CLIENT / "BlueAgentUnity" / relative, project / relative)
    for relative in ("shared", "fixtures"):
        replace_tree(CLIENT / relative, workspace / "app" / "client" / relative)
    # Quest Assets 是覆蓋，不是整目錄替換：否則會刪掉剛複製的共享 Next／模型／字體。
    shutil.copytree(MR / "BlueAgentUnity" / "Assets", project / "Assets",
                    ignore=IGNORE, dirs_exist_ok=True)
    for relative in ("Packages", "ProjectSettings", "Keys"):
        replace_tree(MR / "BlueAgentUnity" / relative, project / relative)
    return project


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=pathlib.Path, help="組合工作區（預設 mr/.build/workspace）")
    args = parser.parse_args()
    build_root = pathlib.Path(os.environ.get("BA_MR_BUILD_ROOT", MR / ".build"))
    workspace = args.workspace or build_root / "workspace"
    project = sync(workspace)
    print(f"✓ MR 構建工作區：{project}")
    print("  共享 Assets／Logic 來自 app/client；Quest 專有 Assets 與設定來自 mr")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
