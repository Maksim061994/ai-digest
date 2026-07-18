#!/bin/sh
# Планировщик контейнера: ежедневный дайджест, ежедневный научный разбор,
# недельный обзор и «живой» вечерний пост раз в пару дней.
#
# Времена дайджеста/разбора/обзора берутся из /app/schedule.conf и перечитываются
# каждую минуту, поэтому бот может менять их на лету (/times, /set). При первом
# запуске файл создаётся из переменных окружения (RUN_AT/PAPER_AT/WEEKLY_AT/
# WEEKLY_DOW из docker-compose.yml). Дальше источник истины — этот файл.
#
# «Живой» пост (vibe.py) запускается в случайную минуту вечернего окна
# [VIBE_WINDOW_START, VIBE_WINDOW_END]; интервал «раз в N дней» стережёт сам vibe.py.
#
# Все запуски — обычные дочерние процессы с ПОЛНЫМ окружением контейнера (в отличие
# от системного cron). Время трактуется по таймзоне контейнера (TZ в docker-compose.yml).
set -u

CONF="/app/schedule.conf"

DEF_RUN_AT="${RUN_AT:-08:00}"
DEF_PAPER_AT="${PAPER_AT:-13:00}"
DEF_WEEKLY_AT="${WEEKLY_AT:-21:00}"
DEF_WEEKLY_DOW="${WEEKLY_DOW:-7}"

VIBE_START="${VIBE_WINDOW_START:-18:00}"
VIBE_END="${VIBE_WINDOW_END:-20:00}"

# HH:MM -> минуты с полуночи (10# — чтобы 08/09 не считались восьмеричными).
to_min() { echo $((10#${1%%:*} * 60 + 10#${1##*:})); }
VIBE_START_MIN=$(to_min "$VIBE_START")
VIBE_END_MIN=$(to_min "$VIBE_END")

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

echo "[scheduler] старт (TZ=$(date +%Z)). Расписание из ${CONF}; живой пост в окне ${VIBE_START}-${VIBE_END}. Изменения применяются в течение минуты."

last_digest=""; last_paper=""; last_weekly=""
vibe_day=""; vibe_target=""; vibe_ran=""

while true; do
  run_at=$(conf_get RUN_AT "$DEF_RUN_AT")
  paper_at=$(conf_get PAPER_AT "$DEF_PAPER_AT")
  weekly_at=$(conf_get WEEKLY_AT "$DEF_WEEKLY_AT")
  weekly_dow=$(conf_get WEEKLY_DOW "$DEF_WEEKLY_DOW")

  today=$(date +%Y-%m-%d)
  hm=$(date +%H:%M)
  dow=$(date +%u)
  now_min=$((10#$(date +%H) * 60 + 10#$(date +%M)))

  # last_* хранит дату последнего запуска — защита от повторов в ту же минуту/день.
  if [ "$hm" = "$run_at" ] && [ "$last_digest" != "$today" ]; then
    last_digest="$today"; run_job "ежедневный дайджест" digest.py
  fi
  if [ "$hm" = "$paper_at" ] && [ "$last_paper" != "$today" ]; then
    last_paper="$today"; run_job "научный разбор статьи" paper.py
  fi
  if [ "$hm" = "$weekly_at" ] && [ "$dow" = "$weekly_dow" ] && [ "$last_weekly" != "$today" ]; then
    last_weekly="$today"; run_job "недельный обзор" digest.py --weekly
  fi

  # «Живой» пост: раз в день выбираем случайную минуту в вечернем окне; интервал
  # «раз в N дней» проверяет сам vibe.py, поэтому запускаем его хоть каждый день.
  if [ "$vibe_day" != "$today" ]; then
    vibe_day="$today"
    vibe_target=$(python3 -c "import random;print(random.randint($VIBE_START_MIN, $VIBE_END_MIN))" 2>/dev/null || echo "$VIBE_START_MIN")
    vibe_ran=""
  fi
  if [ "$now_min" -ge "$vibe_target" ] && [ "$now_min" -le "$VIBE_END_MIN" ] && [ "$vibe_ran" != "$today" ]; then
    vibe_ran="$today"; run_job "живой пост" vibe.py
  fi

  # sleep в фоне + wait: trap срабатывает сразу по сигналу, а не после досыпания.
  sleep 30 &
  wait "$!"
done
