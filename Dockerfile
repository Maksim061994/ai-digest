FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# tzdata — таймзона для планировщика, ca-certificates — TLS для httpx (LLM + Telegram).
# Node.js больше не нужен: LLM вызывается обычным HTTP-запросом, а не CLI-обёрткой.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код (digest.py, channels.txt), планировщик (entrypoint.sh) и сессия Telethon
# монтируются volume'ом (.:/app в docker-compose.yml) — образ остаётся «рантаймом»,
# а правки скриптов подхватываются без пересборки образа.
CMD ["sh", "/app/entrypoint.sh"]
