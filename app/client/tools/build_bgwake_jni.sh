#!/usr/bin/env bash
# 熄屏耳朵的 JNI 薄殼（`app/client/native/bgwake/bgwake_jni.c`）⇒ androidlib 的 libs/arm64-v8a/libblueagent_bgwake.so。
# 用 Unity 自己的 NDK（與 libblueagent_wake.so 同一顆 clang）；連結到同目錄的 libblueagent_wake.so 與
# libsherpa-onnx-c-api.so（NEEDED，執行時由系統 linker 從 app 的 native lib 目錄找）。
# 只有改了 .c 才要重跑；產物進版控旁邊那幾顆 .so 一樣直接放在 libs/ 裡。
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"            # ~/blueagent/unity
# NDK：優先 `ANDROID_NDK_HOME`；沒設 ⇒ Unity 6000.6.5f1 自帶的那份（外部評審 P1-4：別寫死機器與版本）。
# ⚠️ 必須與 libblueagent_wake.so 用**同一顆 clang**（Unity 6000.6.5f1 的 NDK r27c）；換 NDK 就兩顆一起重建。
NDK="${ANDROID_NDK_HOME:-/Applications/Unity/Hub/Editor/6000.6.5f1/PlaybackEngines/AndroidPlayer/NDK}"
HOST="$(uname -s | tr '[:upper:]' '[:lower:]')-x86_64"   # darwin-x86_64／linux-x86_64（Apple Silicon 也是 darwin-x86_64）
CC="$NDK/toolchains/llvm/prebuilt/$HOST/bin/aarch64-linux-android26-clang"
[ -x "$CC" ] || { echo "✗ 找不到 clang：$CC（設 ANDROID_NDK_HOME）"; exit 1; }
LIBS="$HERE/BlueAgentUnity/Assets/Plugins/Android/blueagent-permissions.androidlib/libs/arm64-v8a"
SRC="$HERE/native/bgwake/bgwake_jni.c"
OUT="$LIBS/libblueagent_bgwake.so"
"$CC" -shared -fPIC -O2 -std=c11 -Wall -Wextra -Werror \
  -I"$HERE/../app/android/app/src/main/cpp" \
  -Wl,-soname,libblueagent_bgwake.so -Wl,--no-undefined -Wl,-z,max-page-size=16384 \
  -o "$OUT" "$SRC" -L"$LIBS" -lblueagent_wake -lsherpa-onnx-c-api -lm
echo "✓ $(ls -la "$OUT" | awk '{print $5}') bytes → $OUT"
# 來源記錄（app 倉那幾個檔與每顆 .so 的 sha256）——出包前 `tools/native_sources.sh check` 對拍。
bash "$HERE/tools/native_sources.sh" record
