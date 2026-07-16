#!/bin/sh
# Планировщик контейнера: ежедневный дайджест, ежедневный научный разбор и
# недельный обзор.
#
# Времена берутся из /app/schedule.conf и перечитываются каждую минуту, поэтому
# бот может менять расписание на лету (команды /times, /set). При первом запуске
# schedule.conf создаётся из переменных окружения (RUN_AT/PAPER_AT/WEEKLY_AT/
# WEEKLY_DOW из docker-compose.yml). Дальше источник истины — этот файл.
#
# Все запуски — обычные дочерние процессы с ПОЛНЫМ окружением контейнера, поэтому
# скриптам доступны CLAUDE_CODE_OAUTH_TOKEN и TG_* (в отличие от системного cron).
# Время трактуется по таймзоне контейнера (переменная TZ в docker-compose.yml).
set -u

CONF="/app/schedule.conf"

DEF_RUN_AT="${RUN_AT:-08:00}"
DEF_PAPER_AT="${PAPER_AT:-13:00}"
DEF_WEEKLY_AT="${WEEKLY_AT:-21:00}"
DEF_WEEKLY_DOW="${WEEKLY_DOW:-7}"

# Первичное создание конфига из окружения.
if [ ! -f "$CONF" ]; then
  {
    echo "RUN_AT=$DEF_RUN_AT"
    echo "PAPER_AT=$DEF_PAPER_AT"
    echo "WEEKLY_AT=$DEF_WEEKLY_AT"
    echo "WEEKLY_DOW=$DEF_WEEKLY_DOW"
  } > "$CONF"
fi

# Чистое завершение по сигналу от Docker (иначе shell досыпает sleep -> SIGKILL/137).
trap 'echo "[scheduler] остановка по сигналу"; exit 0' TERM INT

# Безопасное чтение значения из конфига (без source, чтобы не исполнять содержимое).
conf_get() {
  v=$(grep -E "^$1=" "$CONF" 2>/dev/null | tail -n1 | cut -d= -f2 | tr -d ' \r')
  [ -n "$v" ] && echo "$v" || echo "$2"
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

echo "[scheduler] старт (TZ=$(date +%Z)). Расписание из ${CONF}; изменения применяются в течение минуты."

last_digest=""; last_paper=""; last_weekly=""

while true; do
  run_at=$(conf_get RUN_AT "$DEF_RUN_AT")
  paper_at=$(conf_get PAPER_AT "$DEF_PAPER_AT")
  weekly_at=$(conf_get WEEKLY_AT "$DEF_WEEKLY_AT")
  weekly_dow=$(conf_get WEEKLY_DOW "$DEF_WEEKLY_DOW")

  today=$(date +%Y-%m-%d)
  hm=$(date +%H:%M)
  dow=$(date +%u)

  # last_* хранит дату последнего запуска задачи — защита от повторов в ту же минуту/день.
  if [ "$hm" = "$run_at" ] && [ "$last_digest" != "$today" ]; then
    last_digest="$today"; run_job "ежедневный дайджест" digest.py
  fi
  if [ "$hm" = "$paper_at" ] && [ "$last_paper" != "$today" ]; then
    last_paper="$today"; run_job "научный разбор статьи" paper.py
  fi
  if [ "$hm" = "$weekly_at" ] && [ "$dow" = "$weekly_dow" ] && [ "$last_weekly" != "$today" ]; then
    last_weekly="$today"; run_job "недельный обзор" digest.py --weekly
  fi

  # sleep в фоне + wait: trap срабатывает сразу по сигналу, а не после досыпания.
  sleep 30 &
  wait "$!"
done
