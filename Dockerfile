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

# Планировщик — единственное, что зашивается в образ; код (digest.py, channels.txt)
# и сессия Telethon монтируются volume'ом в docker-compose.yml.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

CMD ["entrypoint.sh"]
