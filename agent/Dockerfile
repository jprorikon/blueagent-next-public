# BlueAgent Next —— 單人／多角色鏈路（要取代現役 blueagent-server + blueagent-discord）
#
# 為什麼是 slim + 明確 apt 清單：語音那條路需要 ffmpeg（wav↔PCM）與 libopus（opus 解碼），
# 這兩個是**系統函式庫**，pip 裝不到；其餘全是純 wheel。
#
# 刻意**不裝** faster-whisper：主力是 SenseVoice（現役同一顆，模型用 volume 掛進來），
# 裝 ctranslate2 會讓 image 多 ~600MB 而只為了「SenseVoice 壞掉時的備援」。
# 要備援就在 compose 裡改成 build-arg WITH_WHISPER=1。

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# mDNS 要 nsswitch 有 mdns4_minimal 才會被用到（slim image 預設沒有這行）
RUN printf 'hosts: files mdns4_minimal [NOTFOUND=return] dns\n' > /etc/nsswitch.conf

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg libopus0 ca-certificates tzdata libnss-mdns openssh-client \
 && rm -rf /var/lib/apt/lists/*

ARG WITH_WHISPER=0
# 瀏覽器只裝在各角色自己的 VM；引擎不再背 Playwright／Chromium 的映像與記憶體成本。
COPY agent/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && if [ "$WITH_WHISPER" = "1" ]; then pip install --no-cache-dir faster-whisper; fi

WORKDIR /app
# build context 是倉庫根；容器內照倉庫的樣子擺（/app/agent/…、/app/app/client/assets），包名一律 agent.*／app.*
COPY agent/__init__.py ./agent/__init__.py
COPY agent/brain ./agent/brain
COPY agent/adapter ./agent/adapter
COPY agent/server ./agent/server
# S5：Agent 不携带 App Server；公共区与会话只经远程契约访问。
# C-17：Agent 只带客户端语义目录代码与人格（agent/persona），不携带 104 张客户端贴纸素材。
COPY app/__init__.py ./app/__init__.py
COPY app/client/__init__.py ./app/client/__init__.py
COPY app/client/assets/__init__.py ./app/client/assets/__init__.py
COPY app/client/assets/stickers.py ./app/client/assets/stickers.py
COPY app/client/assets/motion.py ./app/client/assets/motion.py
COPY agent/persona ./agent/persona
COPY agent/client ./agent/client
COPY agent/run.py ./agent/run.py
COPY agent/tests ./agent/tests
# MCP 橋（`data/mcp.json` 會叫它；例如 Firecrawl 的 stdio server）
COPY agent/tools ./agent/tools

# 出處戳記（owner 2026-10-26）：建置時把 git 提交寫進 image label，
# 以後 `docker image inspect` 一眼看得出這顆 image 是哪個 commit 建的，
# 不必再靠「容器內檔案哈希 vs 本地檔」對拍才知道線上是不是庫版本。
# 建置入口是 `agent/tools/deploy_next.sh`（自動算 HEAD，工作樹髒就加 -dirty）；手動 build 沒傳就是 unknown。
# ⚠️ 這個 ARG／LABEL 必須留在**最後**：它每次提交都會變，放在 pip install 之前會讓那一層快取全失效。
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision="${GIT_SHA}" \
      blueagent.git.sha="${GIT_SHA}"

# data/ 由 compose 掛進來（auth token、threads、users/*.md、facts.jsonl）
RUN mkdir -p /app/data /app/out
# PYTHONPATH：MCP 子行程以 /app/agent 為 cwd 跑 `tools/mcp_*.py`，也要能 import agent.*
ENV PYTHONPATH=/app \
    BLUEAGENT_DATA_DIR=/app/data \
    BLUEAGENT_ARTIFACT_DIRS=/app/out \
    BLUEAGENT_CONTAINER_PORT=18790

EXPOSE 18790
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('BLUEAGENT_CONTAINER_PORT','18790')+'/api/health',timeout=4)"

# 容器內必須綁 0.0.0.0：綁 127.0.0.1 的話 docker-proxy 轉不進來（外部連線直接被 reset）
CMD ["python", "-m", "agent.run", "--host", "0.0.0.0", "--port", "18790"]
