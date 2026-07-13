FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# Node.js (нужен Claude Code) + tzdata (таймзона для планировщика)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код (digest.py, channels.txt), планировщик (entrypoint.sh) и сессия Telethon
# монтируются volume'ом (.:/app в docker-compose.yml) — образ остаётся «рантаймом»,
# а правки скриптов подхватываются без пересборки образа.
CMD ["sh", "/app/entrypoint.sh"]
