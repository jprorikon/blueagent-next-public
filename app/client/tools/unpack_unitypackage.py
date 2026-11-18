#!/usr/bin/env python3
"""把 `TMP Essential Resources.unitypackage` 解到 Unity 專案的 Assets/。

為什麼不用 Unity 的 `AssetDatabase.ImportPackage`：它在 `-batchmode -quit` 下是**非同步**的，
`-quit` 可能在匯入完成前就把 Editor 關掉（會得到一個「看起來成功、其實只匯入一半」的專案）。
解包是純檔案動作、可重現、也能讓 NAS 這一側保持真源（版控要收）。

unitypackage 格式：gzip tar，每個資產一個 `<guid>/` 目錄，裡面
`asset`（檔案本體）、`asset.meta`（meta）、`pathname`（要放去哪，相對專案根）——
資料夾也有一筆（只有 `asset.meta` ＋ `pathname`，沒有 `asset`）。
用法：python3 tools/unpack_unitypackage.py <package> <project-root>
"""
import os
import shutil
import sys
import tarfile
import tempfile

SKIP = {"preview.png"}          # 檢視器縮圖，不屬於專案


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    package, project = sys.argv[1], sys.argv[2]
    written = folders = 0
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(package, "r:gz") as tar:
            tar.extractall(tmp)
        for entry in sorted(os.listdir(tmp)):
            base = os.path.join(tmp, entry)
            if not os.path.isdir(base):
                continue
            pathname = os.path.join(base, "pathname")
            if not os.path.isfile(pathname):
                continue
            with open(pathname, encoding="utf-8") as handle:
                rel = handle.read().strip()
            if not rel.startswith("Assets/"):
                print(f"  ! 略過非 Assets 路徑：{rel}")
                continue
            target = os.path.join(project, rel)
            asset = os.path.join(base, "asset")
            meta = os.path.join(base, "asset.meta")
            if os.path.isfile(asset):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(asset, target)
                written += 1
            else:
                os.makedirs(target, exist_ok=True)
                folders += 1
            if os.path.isfile(meta):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(meta, target + ".meta")
    print(f"解包完成：{written} 個檔案、{folders} 個資料夾 ⇒ {project}/Assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
