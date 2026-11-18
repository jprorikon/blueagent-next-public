#!/usr/bin/env bash
# **AndroidManifest.xml 合法性閘門**（本機、秒級，不碰 Mac）
#
# 為什麼要有這支（2026-10-18 付過代價）：
#   `.androidlib` 的 AndroidManifest.xml **註解裡**寫了 `resolve-activity --brief`。
#   XML 規範禁止註解內出現 `--`，但：
#     · 檔案本身「看起來」完全正常，本機也沒人讀它；
#     · Unity 的 `AndroidManifestUtils.Parse()` 一 Parse 就 throw；
#     · 症狀是 `Build result=Failed errors=389`，log 裡**只有 C# 調用棧**（GetPackageFromAndroidManifest），
#       連一行 `error CS` 都沒有 ⇒ 會被誤判成「編譯錯誤」或「Mac 髒快取」，白清一次 Library（十幾分鐘）。
#   ⇒ 建置前在本機先把每個 manifest 交給真正的 XML parser 讀一遍，錯誤當場報行號。
#
# 用法：bash tools/check_android_manifests.sh [Unity 工程目錄]     # exit 0 = 全部合法
# 不帶參數查手機工程；MR 構建時傳組合工作區的工程目錄，兩個 androidlib 一起查。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${1:-$HERE/../BlueAgentUnity}" && pwd)"

# macOS 內建 Bash 3.2 沒有 mapfile；逐行 append 在新舊 Bash 都能跑。
FILES=()
while IFS= read -r manifest; do FILES+=("$manifest"); done \
  < <(find "$ROOT/Assets/Plugins/Android" -name AndroidManifest.xml 2>/dev/null | sort)
if [ "${#FILES[@]}" -eq 0 ]; then
  echo "（沒有找到 .androidlib 的 AndroidManifest.xml，跳過）"
  exit 0
fi

FAIL=0
for f in "${FILES[@]}"; do
  if OUT="$(python3 - "$f" <<'PY' 2>&1
import sys, xml.etree.ElementTree as ET
p = sys.argv[1]
try:
    root = ET.parse(p).getroot()
except ET.ParseError as e:
    print(f"ParseError: {e}")
    sys.exit(1)
if root.tag != "manifest":
    print(f"root 不是 <manifest> 而是 <{root.tag}>")
    sys.exit(1)
if "package" not in root.attrib:
    print("缺 package 屬性（Unity 的 GetPackageFromAndroidManifest 會拋異常）")
    sys.exit(1)
print(f"ok package={root.attrib['package']}")
PY
)"; then
    echo "  ✓ $(basename "$(dirname "$f")")：$OUT"
  else
    echo "  ✗ $f"
    echo "    $OUT"
    FAIL=1
  fi
done

if [ "$FAIL" -ne 0 ]; then
  echo "✗ manifest 不合法 ⇒ 不要建置（Unity 會在 AndroidManifestUtils.Parse 拋異常，整包失敗且無 error CS）"
  exit 1
fi
echo "✓ AndroidManifest.xml 全部合法"
