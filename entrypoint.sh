#!/bin/sh
# Планировщик для контейнера: раз в сутки в RUN_AT запускает дайджест.
#
# Работает как обычный дочерний процесс с ПОЛНЫМ окружением контейнера, поэтому
# запускаемому digest.py доступны CLAUDE_CODE_OAUTH_TOKEN и TG_* (системный cron,
# в отличие от этого, переменные окружения не пробрасывает).
#
# Время трактуется по таймзоне контейнера (переменная TZ в docker-compose.yml).
set -u

RUN_AT="${RUN_AT:-06:00}"

echo "[scheduler] старт. Дайджест — ежедневно в ${RUN_AT} (TZ=$(date +%Z))."

while true; do
  now=$(date +%s)
  next=$(date -d "today ${RUN_AT}" +%s)
  if [ "$next" -le "$now" ]; then
    next=$(date -d "tomorrow ${RUN_AT}" +%s)
  fi
  echo "[scheduler] следующий запуск: $(date -d "@${next}") (через $((next - now))s)"
  sleep "$((next - now))"

  echo "[scheduler] $(date) — запускаю дайджест"
  if (cd /app && python digest.py); then
    echo "[scheduler] готово"
  else
    echo "[scheduler] ОШИБКА: digest.py завершился с кодом $?"
  fi
done
