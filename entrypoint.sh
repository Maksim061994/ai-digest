#!/bin/sh
# Планировщик контейнера: ежедневный дайджест, ежедневный научный разбор и
# недельный обзор по расписанию.
#
# Все запуски — обычные дочерние процессы с ПОЛНЫМ окружением контейнера, поэтому
# скриптам доступны CLAUDE_CODE_OAUTH_TOKEN и TG_* (системный cron, в отличие от
# этого, переменные окружения не пробрасывает).
#
# Время трактуется по таймзоне контейнера (переменная TZ в docker-compose.yml).
#   RUN_AT     — ежедневный дайджест (HH:MM), по умолчанию 06:00
#   PAPER_AT   — ежедневный научный разбор статьи (HH:MM), по умолчанию 13:00
#   WEEKLY_AT  — недельный обзор (HH:MM), по умолчанию 21:00
#   WEEKLY_DOW — день недели обзора по `date +%u`: 1=Пн … 7=Вс, по умолчанию 7
set -u

DAILY_AT="${RUN_AT:-06:00}"
PAPER_AT="${PAPER_AT:-13:00}"
WEEKLY_AT="${WEEKLY_AT:-21:00}"
WEEKLY_DOW="${WEEKLY_DOW:-7}"

# Чистое завершение по сигналу от Docker (иначе shell досыпает sleep и получает SIGKILL/137).
trap 'echo "[scheduler] остановка по сигналу"; exit 0' TERM INT

echo "[scheduler] старт (TZ=$(date +%Z)). Дайджест в ${DAILY_AT}; разбор статьи в ${PAPER_AT}; недельный обзор в ${WEEKLY_AT} (день недели ${WEEKLY_DOW})."

# Ближайший будущий момент HH:MM (epoch). $1 = время, $2 = now.
next_at() {
  t=$(date -d "today $1" +%s)
  [ "$t" -le "$2" ] && t=$(date -d "tomorrow $1" +%s)
  echo "$t"
}

# Ближайший будущий WEEKLY_AT в день недели WEEKLY_DOW (epoch). $1 = now.
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

run_job() {
  label="$1"; shift
  echo "[scheduler] $(date) — ${label}"
  if (cd /app && python "$@"); then
    echo "[scheduler] ${label}: готово"
  else
    echo "[scheduler] ${label}: ОШИБКА (код $?)"
  fi
}

# Наименьшее из непустых значений
min() {
  m=""
  for v in "$@"; do
    [ -z "$v" ] && continue
    { [ -z "$m" ] || [ "$v" -lt "$m" ]; } && m="$v"
  done
  echo "$m"
}

while true; do
  now=$(date +%s)
  nd=$(next_at "$DAILY_AT" "$now")
  np=$(next_at "$PAPER_AT" "$now")
  nw=$(next_weekly "$now")

  target=$(min "$nd" "$np" "$nw")
  echo "[scheduler] следующее событие: $(date -d "@${target}") (через $((target - now))s)"
  # sleep в фоне + wait: так trap срабатывает сразу по сигналу, а не после досыпания.
  sleep "$((target - now))" &
  wait "$!"

  now=$(date +%s)
  [ "$now" -ge "$nd" ] && run_job "ежедневный дайджест" digest.py
  [ "$now" -ge "$np" ] && run_job "научный разбор статьи" paper.py
  [ -n "$nw" ] && [ "$now" -ge "$nw" ] && run_job "недельный обзор" digest.py --weekly
done
