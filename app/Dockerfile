# 独立 App Server：只包含平台代码，不复制 Agent、模型或旧 data/。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BLUEAGENT_APP_HOST=0.0.0.0 \
    BLUEAGENT_APP_PORT=8788 \
    BLUEAGENT_APP_DATA=/app/data \
    BLUEAGENT_APP_ACCOUNTS=/app/data/accounts.json

# ffmpeg：媒体与贴纸的 `w=192` 缩图靠它（`app/server/media.py::thumbnail`）。没装就一律回原图，
# 手机端每张图都在下原图（PENDING B.App.5，2026-11-16 补上）。--no-install-recommends 压体积。
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# 只打包平台服务与其所需的客户端素材；不携带 Unity 工程、fixtures 或测试缓存。
COPY app/__init__.py ./app/__init__.py
COPY app/server ./app/server
COPY app/client/__init__.py ./app/client/__init__.py
COPY app/client/assets ./app/client/assets
RUN mkdir -p /app/data

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision="${GIT_SHA}" \
      blueagent.git.sha="${GIT_SHA}"

EXPOSE 8788
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8788/health',timeout=4)"

CMD ["python", "-m", "app.server"]
