FROM python:3.11-slim

# 不写 .pyc、stdout/stderr 直出
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app

# 数据卷：SQLite 文件默认落在 /data/config.db（可用 CONFIG_DB_PATH 覆盖）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 服务启动时由 lifespan 自动建表，无需额外初始化命令
HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=5 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('CONFIG_PORT','8000')+'/health')" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host ${CONFIG_HOST:-0.0.0.0} --port ${CONFIG_PORT:-8000}"]
