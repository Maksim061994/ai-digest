FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# Node.js (нужен Claude Code), tzdata (таймзона), supercronic (cron внутри контейнера)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && curl -fsSL -o /usr/local/bin/supercronic \
        https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64 \
    && chmod +x /usr/local/bin/supercronic \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код (digest.py, channels.txt) и сессия Telethon монтируются как volume в compose,
# поэтому здесь их не копируем — образ остаётся чистым «рантаймом».

CMD ["supercronic", "/app/crontab"]
