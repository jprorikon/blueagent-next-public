#!/usr/bin/env bash
# **原生 .so 的來源記錄／對拍**（外部評審 P1-4，2026-10-18）。
#
# 問題：`libblueagent_wake.so`（喚醒引擎）與 `libblueagent_bgwake.so`（熄屏 JNI 薄殼）的原始碼／標頭在
# **另一個倉**（`blueagent/app` 的 `android/app/src/main/cpp/`）。那邊一改，這裡進版控的 .so 就**靜默和源碼不一致**
# ——症狀只是「分數不對」，極難查。
#
# 做法：建完 .so 之後 `record` 一次，把「用的是 app 倉哪一版、哪幾個檔的 sha256、每顆 .so 的 sha256」寫進
# `libs/arm64-v8a/SOURCES.txt`（跟 .so 一起進版控）；之後發版／出包前跑 `check`：app 倉那幾個檔或 .so 本身
# 跟記錄不一樣 ⇒ 非零退出，提醒要重建。
#
# 用法：bash tools/native_sources.sh record   # 重建 .so 之後
#       bash tools/native_sources.sh check    # 出包前（不一致 ⇒ exit 1）
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"            # ~/blueagent/unity
APP="${BLUEAGENT_APP_REPO:-$HERE/../app}"
CPP_REL="android/app/src/main/cpp"
LIBS="$HERE/BlueAgentUnity/Assets/Plugins/Android/blueagent-permissions.androidlib/libs/arm64-v8a"
OUT="$LIBS/SOURCES.txt"
SOURCES=(wake_core.cpp wake_core.h wake_c_api.cpp wake_c_api.h)
SOS=(libblueagent_wake.so libblueagent_bgwake.so libsherpa-onnx-c-api.so libonnxruntime.so)

# `shasum`（macOS／多數 Linux）不在 ⇒ 退 `sha256sum`（精簡 Linux／CI；外部評審 v2 B2）
if command -v shasum >/dev/null 2>&1; then
  sha() { shasum -a 256 "$1" | awk '{print $1}'; }
else
  sha() { sha256sum "$1" | awk '{print $1}'; }
fi

current() {
  echo "# 原生 .so 的來源記錄（tools/native_sources.sh record 產生；勿手改）"
  echo "app_repo_commit $(git -C "$APP" log -1 --format=%H -- "$CPP_REL" 2>/dev/null || echo unknown)"
  for f in "${SOURCES[@]}"; do echo "src $CPP_REL/$f $(sha "$APP/$CPP_REL/$f")"; done
  echo "src native/bgwake/bgwake_jni.c $(sha "$HERE/native/bgwake/bgwake_jni.c")"
  for f in "${SOS[@]}"; do echo "so $f $(sha "$LIBS/$f")"; done
  echo "# libsherpa-onnx-c-api.so／libonnxruntime.so：自 sherpa-onnx 的 Android AAR 抽出（arm64-v8a），不在這裡建。"
}

case "${1:-check}" in
  record)
    current > "$OUT"
    echo "✓ 記下 → $OUT"
    ;;
  check)
    [ -f "$OUT" ] || { echo "✗ 沒有 $OUT（先 record）"; exit 1; }
    want="$(mktemp)"; have="$(mktemp)"; trap 'rm -f "$want" "$have"' EXIT
    grep -v '^#' "$OUT" | grep -v '^app_repo_commit' > "$want"
    current | grep -v '^#' | grep -v '^app_repo_commit' > "$have"
    if diff "$want" "$have" >/dev/null; then
      echo "✓ 原生 .so 與記錄的來源一致"
    else
      echo "✗ 原生 .so 或 app 倉的源碼跟記錄不一致（要重建 .so 再 record）："
      diff "$want" "$have" || true
      exit 1
    fi
    ;;
  *) echo "usage: $0 record|check"; exit 2 ;;
esac
