#!/usr/bin/env bash
# 在線測試：拿真的橋（本機 :8900）的 /chat 串流餵 C# 的 SseStream 切分器。
# token 從 docker/blueagent-server/.env 讀（不進聊天、不落檔）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNITY_REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$UNITY_REPO/../../.." && pwd)"
export PATH="$HOME/.dotnet:$PATH"
export BA_SSE_LIVE=1
export BA_BRIDGE_TOKEN="$(grep '^STREAM_TOKEN=' "$WS/docker/blueagent-server/.env" | cut -d= -f2-)"
export BA_BRIDGE_URL="${BA_BRIDGE_URL:-http://127.0.0.1:8900}"
cd "$UNITY_REPO"
dotnet test shared/BlueAgent.Logic.Tests/BlueAgent.Logic.Tests.csproj --nologo \
  --filter "FullyQualifiedName~SseStreamTest" --logger "console;verbosity=detailed" 2>&1 \
  | grep -E "在線：|^  (read|started|delta|done|thinking|speech|error)|Passed!|Failed|Skipped|frames"
