#!/bin/sh
# Планировщик контейнера: ежедневный дайджест + недельный обзор по расписанию.
#
# Оба запуска — обычные дочерние процессы с ПОЛНЫМ окружением контейнера, поэтому
# digest.py видит CLAUDE_CODE_OAUTH_TOKEN и TG_* (системный cron, в отличие от
# этого, переменные окружения не пробрасывает).
#
# Время трактуется по таймзоне контейнера (переменная TZ в docker-compose.yml).
#   RUN_AT     — ежедневный дайджест (HH:MM), по умолчанию 06:00
#   WEEKLY_AT  — недельный обзор (HH:MM), по умолчанию 21:00
#   WEEKLY_DOW — день недели обзора по `date +%u`: 1=Пн … 7=Вс, по умолчанию 7
set -u

DAILY_AT="${RUN_AT:-06:00}"
WEEKLY_AT="${WEEKLY_AT:-21:00}"
WEEKLY_DOW="${WEEKLY_DOW:-7}"

echo "[scheduler] старт (TZ=$(date +%Z)). Дайджест ежедневно в ${DAILY_AT}; недельный обзор в ${WEEKLY_AT} (день недели ${WEEKLY_DOW})."

# Ближайший будущий момент DAILY_AT (epoch)
next_daily() {
  t=$(date -d "today ${DAILY_AT}" +%s)
  [ "$t" -le "$1" ] && t=$(date -d "tomorrow ${DAILY_AT}" +%s)
  echo "$t"
}

# Ближайший будущий WEEKLY_AT в день недели WEEKLY_DOW (epoch)
next_weekly() {
  i=0
  while [ "$i" -le 7 ]; do
    day=$(date -d "+${i} day" +%Y-%m-%d)
    cand=$(date -d "${day} ${WEEKLY_AT}" +%s)
    if [ "$cand" -gt "$1" ] && [ "$(date -d "@${cand}" +%u)" = "${WEEKLY_DOW}" ]; then
      echo "$cand"; return
    fi
    i=$((i + 1))
  done
}

run_digest() {
  label="$1"; shift
  echo "[scheduler] $(date) — ${label}"
  if (cd /app && python digest.py "$@"); then
    echo "[scheduler] ${label}: готово"
  else
    echo "[scheduler] ${label}: ОШИБКА (код $?)"
  fi
}

while true; do
  now=$(date +%s)
  nd=$(next_daily "$now")
  nw=$(next_weekly "$now")

  if [ -n "$nw" ] && [ "$nw" -lt "$nd" ]; then
    target=$nw
  else
    target=$nd
  fi
  echo "[scheduler] следующее событие: $(date -d "@${target}") (через $((target - now))s)"
  sleep "$((target - now))"

  now=$(date +%s)
  [ "$now" -ge "$nd" ] && run_digest "ежедневный дайджест"
  [ -n "$nw" ] && [ "$now" -ge "$nw" ] && run_digest "недельный обзор" --weekly
done
