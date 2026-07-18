#!/usr/bin/env python3
"""
Разовый скрипт: собрать посты каналов за последние N месяцев и свести их в
итоговый markdown-документ о ключевых трендах в ИИ (со ссылками на источники).

Из-за объёма (за 3 месяца постов слишком много для одного запроса) работает
по схеме map-reduce: посты бьются на батчи, каждый сжимается в список значимого
со ссылками, затем из выжимок синтезируется финальный отчёт о трендах.

Готовый файл (по умолчанию trends_report.md) бот присылает по команде /trends.

Запуск: python trends.py                     — за 3 месяца в trends_report.md
        python trends.py --months 3 --out trends_report.md
"""
import argparse
import os
from datetime import datetime, timedelta

import digest  # переиспользуем сбор постов и вызов claude

TIMEZONE = digest.TIMEZONE
BASE_DIR = digest.BASE_DIR

TRENDS_MODEL = os.environ.get(
    "TRENDS_MODEL", os.environ.get("PAPER_MODEL", os.environ.get("CLAUDE_MODEL", "sonnet")))
MSG_LIMIT = int(os.environ.get("TRENDS_MSG_LIMIT", "2000"))  # максимум сообщений на канал
POST_CHARS = 500          # обрезка поста для сведения (тренды, не дословно)
BATCH_CHARS = 60000       # размер батча для map-этапа


MAP_PROMPT = """Ниже посты из отраслевых Telegram-каналов про ИИ за часть периода. Составь КОМПАКТНЫЙ markdown-список только по-настоящему значимого: релизы моделей и продуктов, исследования, крупные сделки и события индустрии, регулирование, заметные инструменты и open source. По каждому пункту — суть в одной фразе и ссылка(и) на источник (формат https://t.me/...). Объединяй дубли (одна новость из разных каналов — один пункт со всеми ссылками). Пропускай рекламу, анонсы вебинаров, мемы и мелочь. Не выдумывай того, чего нет в постах.

Посты:
{posts}
"""

REDUCE_PROMPT = """Ниже собранные за {period} ключевые события из отраслевых ИИ-каналов (markdown-выжимки со ссылками). Составь ИТОГОВЫЙ аналитический документ о ГЛАВНЫХ ТРЕНДАХ в ИИ прямо сейчас.

Требования:
- Выдели 5–10 ключевых трендов. Каждый — с заголовком уровня ## и 2–4 предложениями: что происходит, почему это важно, куда движется.
- По каждому тренду приведи 2–5 показательных ссылок-источников ИЗ СПИСКА ниже (сохраняй реальные ссылки, не выдумывай).
- В начале — короткое введение (2–3 предложения) с общей картиной.
- В конце — раздел «## Коротко о главном» с 3–5 выводами-тезисами.
- Строго, по существу, без воды и без выдуманных фактов. По-русски. Формат — чистый markdown.

Материал (выжимки со ссылками):
{summaries}
"""


def make_batches(lines, budget=BATCH_CHARS):
    batches, cur, n = [], [], 0
    for ln in lines:
        if n + len(ln) > budget and cur:
            batches.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        batches.append("\n".join(cur))
    return batches


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=3, help="за сколько месяцев (по умолчанию 3)")
    parser.add_argument("--out", default="trends_report.md", help="куда сохранить отчёт")
    args = parser.parse_args()

    now = datetime.now(TIMEZONE)
    day_end = now
    day_start = now - timedelta(days=30 * args.months)
    period = f"{day_start.strftime('%d.%m.%Y')} — {day_end.strftime('%d.%m.%Y')}"

    print(f"[info] собираю посты за {period} из {len(digest.CHANNELS)} каналов "
          f"(до {MSG_LIMIT} на канал)…")
    posts = digest.asyncio.run(digest.collect_posts(day_start, day_end, limit=MSG_LIMIT))
    print(f"[info] всего собрано: {len(posts)} постов")
    if not posts:
        print("[info] постов нет — отчёт не формируется")
        return

    posts.sort(key=lambda p: p["datetime"])
    lines = [f"[{p['datetime'][:10]}] @{p['channel']}: {p['text'][:POST_CHARS]} — {p['link']}"
             for p in posts]
    batches = make_batches(lines)
    print(f"[info] map-этап: {len(batches)} батч(ей) через claude ({TRENDS_MODEL})…")

    summaries = []
    for i, batch in enumerate(batches, 1):
        print(f"[info]   батч {i}/{len(batches)}…")
        summaries.append(digest._run_claude(MAP_PROMPT.format(posts=batch), TRENDS_MODEL))

    print("[info] reduce-этап: синтез итогового документа…")
    report_body = digest._run_claude(
        REDUCE_PROMPT.format(period=period, summaries="\n\n".join(summaries)),
        TRENDS_MODEL)

    header = (f"# Ключевые тренды в ИИ\n\n"
              f"_Период: {period}. Источников: {len(digest.CHANNELS)} каналов, "
              f"обработано {len(posts)} постов. Сформировано {now.strftime('%d.%m.%Y')}._\n\n")
    out_path = BASE_DIR / args.out
    out_path.write_text(header + report_body.strip() + "\n", encoding="utf-8")
    print(f"[info] готово ✅  Отчёт: {out_path}")
    print("[info] пришлите его в Telegram командой боту: /trends")


if __name__ == "__main__":
    main()
