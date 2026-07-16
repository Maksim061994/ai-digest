#!/usr/bin/env python3
"""
AI News Digest: собирает вчерашние посты из открытых Telegram-каналов,
суммаризирует их через Claude Code (headless, по подписке Max) и публикует
дайджест в целевой Telegram-канал.

Запуск: python digest.py            — дайджест за вчера
        python digest.py --dry-run  — собрать и суммаризировать, но не постить
        python digest.py --date 2026-07-03 — дайджест за конкретную дату
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------- настройки

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TARGET_CHANNEL = os.environ["TG_TARGET_CHANNEL"]      # @my_digest_channel или -100...
TIMEZONE = ZoneInfo(os.environ.get("DIGEST_TZ", "Europe/Moscow"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
SESSION_FILE = str(BASE_DIR / "digest_session")

# Список каналов-источников: по одному username на строку в channels.txt
CHANNELS = [
    line.strip().lstrip("@")
    for line in (BASE_DIR / "channels.txt").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.strip().startswith("#")
]

MAX_POST_CHARS = 2500        # обрезка очень длинных постов перед отправкой в LLM
TG_MESSAGE_LIMIT = 4096      # лимит Telegram на одно сообщение

HISTORY_DIR = BASE_DIR / "history"   # сюда сохраняются опубликованные дайджесты
# Сколько прошлых выпусков показывать модели, чтобы не повторять вчерашние новости
LOOKBACK_DAYS = int(os.environ.get("DEDUP_LOOKBACK_DAYS", "2"))

# ---------------------------------------------------------------- сбор постов


async def collect_posts(day_start: datetime, day_end: datetime) -> list[dict]:
    """Собирает посты из всех каналов за интервал [day_start, day_end)."""
    posts = []
    async with TelegramClient(SESSION_FILE, API_ID, API_HASH) as client:
        for channel in CHANNELS:
            try:
                entity = await client.get_entity(channel)
            except Exception as e:
                print(f"[warn] не удалось открыть @{channel}: {e}", file=sys.stderr)
                continue

            count = 0
            # iter_messages идёт от новых к старым; offset_date=day_end отсекает сегодняшние
            async for msg in client.iter_messages(entity, offset_date=day_end, limit=300):
                msg_dt = msg.date.astimezone(TIMEZONE)
                if msg_dt < day_start:
                    break
                text = (msg.text or "").strip()
                if len(text) < 80:          # пропускаем стикеры, «👍», короткие реплики
                    continue
                # часть альбома: текст обычно только у первого сообщения — остальные отсеются по длине
                posts.append({
                    "channel": channel,
                    "link": f"https://t.me/{channel}/{msg.id}",
                    "datetime": msg_dt.isoformat(timespec="minutes"),
                    "text": text[:MAX_POST_CHARS],
                })
                count += 1
            print(f"[info] @{channel}: {count} постов")
            await asyncio.sleep(1.5)        # бережём rate limits
    return posts


# ------------------------------------------------------ история / дедупликация


def load_recent_digests(before_date, n: int) -> list[tuple]:
    """До n последних опубликованных дайджестов с датой раньше before_date."""
    if n <= 0 or not HISTORY_DIR.exists():
        return []
    items = []
    for f in HISTORY_DIR.glob("*.html"):
        try:
            d = datetime.strptime(f.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < before_date:
            items.append((d, f))
    items.sort(key=lambda x: x[0])
    return [(d, f.read_text(encoding="utf-8")) for d, f in items[-n:]]


def save_digest(day_start: datetime, text: str) -> None:
    """Сохраняет опубликованный дайджест в history/YYYY-MM-DD.html."""
    HISTORY_DIR.mkdir(exist_ok=True)
    (HISTORY_DIR / f"{day_start.strftime('%Y-%m-%d')}.html").write_text(
        text, encoding="utf-8")


def build_history_block(previous: list[tuple]) -> str:
    """Формирует вставку в промпт с прошлыми выпусками (или пустую строку)."""
    if not previous:
        return ""
    parts = [f"— Выпуск за {d.strftime('%d.%m.%Y')}:\n{text}"
             for d, text in previous]
    joined = "\n\n".join(parts)
    return (
        "\nВАЖНО — не повторяйся с прошлыми выпусками. Ниже уже опубликованные "
        "дайджесты за предыдущие дни. Не включай новости, которые в них уже "
        "освещены: пропускай те же события и их продолжения без существенного "
        "развития. Старую тему бери только при значимо новой информации и "
        "подавай явно как обновление.\n\n"
        "=== РАНЕЕ ОПУБЛИКОВАНО ===\n"
        f"{joined}\n"
        "=== КОНЕЦ ПРОШЛЫХ ВЫПУСКОВ ===\n"
    )


# ------------------------------------------------------------- саммаризация


PROMPT_TEMPLATE = """Ты — редактор ежедневного дайджеста новостей об ИИ для Telegram-канала.
Ниже JSON-массив постов из отраслевых каналов за {date_human}.
{history_block}
Твоя задача — вернуть ГОТОВЫЙ ТЕКСТ дайджеста и ничего больше (без преамбул, без markdown-заборов):

1. Дедуплицируй: одну и ту же новость часто постят несколько каналов — объедини в один пункт, ссылки на все источники перечисли в конце пункта.
2. Сгруппируй новости по темам. Используй только реально наполненные группы из списка: 🚀 Релизы моделей и продуктов, 🔬 Исследования и статьи, 💼 Бизнес и индустрия, ⚖️ Регулирование и политика, 🛠 Инструменты и open source, 📰 Прочее.
3. По каждой новости: жирный мини-заголовок, затем 1–2 предложения сути, затем ссылки.
4. Отбрасывай рекламу, анонсы вебинаров каналов, мемы и посты без новостной ценности.
5. Пиши по-русски, сжато, без воды. Не выдумывай факты, которых нет в постах.

Формат — HTML для Telegram (только теги <b>, <i>, <a href="...">):

<b>🤖 ИИ-дайджест за {date_human}</b>

<b>🚀 Релизы моделей и продуктов</b>

<b>Название новости.</b> Суть в 1–2 предложениях. <a href="ССЫЛКА">Источник</a>

(и так далее по группам)

В конце строка: <i>Всего обработано {n_posts} постов из {n_channels} каналов.</i>

Если пунктов больше ~25 — оставь только самые значимые.

Посты:
{posts_json}
"""


def _run_claude(prompt: str, model: str | None = None) -> str:
    """Гоняет промпт через claude -p (headless, по подписке) и чистит ответ."""
    env = os.environ.copy()
    # КРИТИЧНО: если задан ANTHROPIC_API_KEY, claude -p начнёт списывать деньги
    # с API-аккаунта вместо подписки Max. Убираем принудительно.
    env.pop("ANTHROPIC_API_KEY", None)

    result = subprocess.run(
        [
            "claude", "-p",
            "--model", model or CLAUDE_MODEL,
            "--output-format", "json",
            "--max-turns", "1",          # чистая генерация, инструменты не нужны
            "--disallowedTools", "Bash,Edit,Write,Read,WebSearch,WebFetch",
        ],
        input=prompt,
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p завершился с ошибкой:\n{result.stderr[-2000:]}")

    payload = json.loads(result.stdout)
    text = payload.get("result", "").strip()
    if not text:
        raise RuntimeError(f"Пустой ответ от Claude: {result.stdout[:500]}")
    # на случай, если модель всё же обернула ответ в ```
    return re.sub(r"^```(?:html)?\s*|\s*```$", "", text)


def summarize_with_claude(posts: list[dict], date_human: str,
                          previous: list[tuple] | None = None) -> str:
    """Дневной дайджест: суммаризирует посты за день."""
    prompt = PROMPT_TEMPLATE.format(
        date_human=date_human,
        n_posts=len(posts),
        n_channels=len({p["channel"] for p in posts}),
        history_block=build_history_block(previous or []),
        posts_json=json.dumps(posts, ensure_ascii=False, indent=1),
    )
    return _run_claude(prompt)


WEEKLY_PROMPT_TEMPLATE = """Ты — редактор еженедельного обзора новостей об ИИ для Telegram-канала.
Ниже — уже опубликованные ежедневные дайджесты за прошедшую неделю ({week_human}).
На их основе собери ОБЗОР КЛЮЧЕВЫХ НОВОСТЕЙ НЕДЕЛИ.

Верни ГОТОВЫЙ ТЕКСТ и ничего больше (без преамбул, без markdown-заборов):

1. Отбери только по-настоящему значимое за неделю — не пересказывай всё подряд (ориентир: 8–15 пунктов).
2. Объединяй связанные события недели в один пункт, показывай развитие сюжета за неделю.
3. Сгруппируй по темам: 🚀 Релизы моделей и продуктов, 🔬 Исследования, 💼 Бизнес и индустрия, ⚖️ Регулирование, 🛠 Инструменты и open source. Используй только наполненные группы.
4. По каждому пункту: жирный мини-заголовок, 1–2 предложения сути, ссылки-источники (бери их из дайджестов).
5. Пиши по-русски, сжато. Не выдумывай фактов, которых нет в дайджестах.

Формат — HTML для Telegram (только теги <b>, <i>, <a href="...">):

<b>📅 Итоги недели в ИИ ({week_human})</b>

<b>🚀 Релизы моделей и продуктов</b>

<b>Название.</b> Суть в 1–2 предложениях. <a href="ССЫЛКА">Источник</a>

(и так далее по группам)

В конце строка: <i>Обзор собран из {n_days} ежедневных дайджестов.</i>

Ежедневные дайджесты за неделю:
{digests}
"""


def summarize_weekly(digests: list[tuple], week_human: str) -> str:
    """Недельный обзор: суммаризирует ежедневные дайджесты за неделю."""
    body = "\n\n".join(
        f"=== Дайджест за {d.strftime('%d.%m.%Y')} ===\n{text}" for d, text in digests)
    prompt = WEEKLY_PROMPT_TEMPLATE.format(
        week_human=week_human, n_days=len(digests), digests=body)
    return _run_claude(prompt)


# ---------------------------------------------------------------- публикация


def split_for_telegram(text: str, limit: int = TG_MESSAGE_LIMIT) -> list[str]:
    """Режет текст на части < limit, стараясь резать по пустым строкам."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = (current + "\n\n" + block).strip()
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def post_to_channel(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chunk in split_for_telegram(text):
        r = httpx.post(url, json={
            "chat_id": TARGET_CHANNEL,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=30)
        data = r.json()
        if not data.get("ok"):
            # частая причина — невалидный HTML; fallback без parse_mode
            print(f"[warn] HTML-отправка не удалась: {data}. Пробую как plain text.",
                  file=sys.stderr)
            plain = re.sub(r"<[^>]+>", "", chunk)
            r2 = httpx.post(url, json={
                "chat_id": TARGET_CHANNEL,
                "text": plain,
                "disable_web_page_preview": True,
            }, timeout=30)
            r2.raise_for_status()


# --------------------------------------------------------------------- main


def run_daily(args) -> None:
    """Ежедневный дайджест за день (по умолчанию — за вчера)."""
    if args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TIMEZONE)
    else:
        day = (datetime.now(TIMEZONE) - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    date_human = day_start.strftime("%d.%m.%Y")

    print(f"[info] собираю посты за {date_human} из {len(CHANNELS)} каналов…")
    posts = asyncio.run(collect_posts(day_start, day_end))
    print(f"[info] всего собрано: {len(posts)} постов")

    if not posts:
        print("[info] постов нет — дайджест не публикуется")
        return

    previous = load_recent_digests(day_start.date(), LOOKBACK_DAYS)
    if previous:
        days = ", ".join(d.strftime("%d.%m") for d, _ in previous)
        print(f"[info] учитываю прошлые выпуски для дедупликации: {days}")

    print("[info] суммаризирую через claude -p…")
    digest = summarize_with_claude(posts, date_human, previous)

    if args.dry_run:
        print("\n" + "=" * 60 + "\n" + digest)
        return

    print("[info] публикую в канал…")
    post_to_channel(digest)
    save_digest(day_start, digest)
    print("[info] готово ✅")


def run_weekly(args) -> None:
    """Недельный обзор: ключевые новости из сохранённых дайджестов за неделю."""
    if args.date:
        today = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TIMEZONE)
    else:
        today = datetime.now(TIMEZONE)

    # берём до 7 последних дайджестов с датой по сегодняшний день включительно
    digests = load_recent_digests(today.date() + timedelta(days=1), 7)
    if not digests:
        print("[info] нет сохранённых дайджестов за неделю — обзор не формируется")
        return

    week_human = (f"{digests[0][0].strftime('%d.%m')}–"
                  f"{digests[-1][0].strftime('%d.%m.%Y')}")
    print(f"[info] недельный обзор по {len(digests)} дайджестам ({week_human})…")
    review = summarize_weekly(digests, week_human)

    if args.dry_run:
        print("\n" + "=" * 60 + "\n" + review)
        return

    print("[info] публикую недельный обзор…")
    post_to_channel(review)
    print("[info] готово ✅")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="не постить, вывести результат в stdout")
    parser.add_argument("--date",
                        help="дата YYYY-MM-DD: для дайджеста — за какой день, "
                             "для --weekly — конец недели (по умолчанию сегодня/вчера)")
    parser.add_argument("--weekly", action="store_true",
                        help="недельный обзор ключевых новостей из сохранённых дайджестов")
    args = parser.parse_args()

    if args.weekly:
        run_weekly(args)
    else:
        run_daily(args)


if __name__ == "__main__":
    main()
