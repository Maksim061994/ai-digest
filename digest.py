#!/usr/bin/env python3
"""
AI News Digest: собирает вчерашние посты из открытых Telegram-каналов,
суммаризирует их через Gemini API (OpenAI-совместимый эндпоинт) и публикует
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
import sys
import time
from contextlib import contextmanager
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
SESSION_FILE = str(BASE_DIR / "digest_session")

# ------------------------------------------------------------------ LLM
# Провайдер задаётся двумя переменными, поэтому смена движка (Gemini -> DeepSeek,
# z.ai, OpenRouter) не требует правок кода — все они говорят на одном
# OpenAI-совместимом /chat/completions.
LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "16384"))
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "600"))
# none|low|medium|high — пусто означает «не передавать параметр» (для провайдеров,
# которые его не понимают и отвечают 400).
LLM_REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "low").strip()

# --------------------------------------------------------------- уведомления
# Личный chat_id владельца для алертов о сбоях. Намеренно отдельная переменная,
# а не BOT_ADMINS: админы управляют списком каналов, а про умерший ключ и
# кончившуюся квоту должен узнавать тот, кто платит. Свой id можно узнать,
# написав что угодно боту — он ответит «Ваш id: ...».
ALERT_CHAT_ID = os.environ.get("ALERT_CHAT_ID", "").strip()
# Один и тот же сбой повторяется каждый запуск (дайджест, разбор, вечерний пост),
# поэтому одинаковые алерты шлём не чаще раза в N часов.
ALERT_REPEAT_HOURS = float(os.environ.get("ALERT_REPEAT_HOURS", "12"))
ALERT_STATE = BASE_DIR / "alert_state.json"

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


# --------------------------------------------------------- алерты о сбоях


class LLMError(RuntimeError):
    """Ошибка обращения к модели с разобранной причиной.

    `kind` — короткий машинный признак (key / quota / billing / model / ...),
    по нему же группируются повторные уведомления.
    """

    def __init__(self, message: str, kind: str = "unknown", detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def notify_owner(text: str) -> None:
    """Шлёт личное сообщение владельцу. Не бросает исключений: уведомление —
    побочный эффект, и его поломка не должна подменять исходную ошибку.

    Отправляется без parse_mode: в тексте бывают угловые скобки из ответа API,
    и с HTML-разметкой Telegram отверг бы такое сообщение целиком.
    """
    if not ALERT_CHAT_ID:
        print("[warn] ALERT_CHAT_ID не задан — уведомление не отправлено", file=sys.stderr)
        return
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ALERT_CHAT_ID, "text": text[:4000],
                  "disable_web_page_preview": True},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[warn] уведомление не ушло: {r.text[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] уведомление не ушло: {e!r}", file=sys.stderr)


def _alert_throttled(key: str) -> bool:
    """True, если такой же алерт уже отправляли меньше ALERT_REPEAT_HOURS назад."""
    now = time.time()
    try:
        state = json.loads(ALERT_STATE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    if now - float(state.get(key, 0)) < ALERT_REPEAT_HOURS * 3600:
        return True
    state[key] = now
    try:
        ALERT_STATE.write_text(json.dumps(state), encoding="utf-8")
    except Exception as e:
        print(f"[warn] не сохранил состояние алертов: {e!r}", file=sys.stderr)
    return False


@contextmanager
def alert_on_failure(job: str):
    """Ловит падение задачи, шлёт владельцу диагноз и пробрасывает исключение
    дальше — чтобы код возврата остался ненулевым и планировщик записал сбой."""
    try:
        yield
    except Exception as e:
        if isinstance(e, LLMError):
            kind, summary, detail = e.kind, str(e), e.detail
        else:
            kind, summary, detail = f"other:{type(e).__name__}", f"{type(e).__name__}: {e}", ""

        if _alert_throttled(f"{job}:{kind}"):
            print(f"[info] про «{kind}» уже уведомляли — молчу", file=sys.stderr)
        else:
            lines = [f"🔴 ai-digest: {job} — сбой", "", summary]
            if detail:
                lines += ["", detail[:600]]
            lines += ["",
                      f"Время: {datetime.now(TIMEZONE).strftime('%d.%m.%Y %H:%M')}",
                      f"Повтор такого уведомления — не чаще раза в "
                      f"{ALERT_REPEAT_HOURS:g} ч."]
            notify_owner("\n".join(lines))
        raise


# ---------------------------------------------------------------- сбор постов


async def collect_posts(day_start: datetime, day_end: datetime,
                        limit: int = 300) -> list[dict]:
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
            async for msg in client.iter_messages(entity, offset_date=day_end, limit=limit):
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

1. Начни дайджест с живой вводки на 2–4 предложения обычным связным текстом. Во вводке скажи, что сегодня главное, как связаны сюжеты дня и на что стоит смотреть. Тон взвешенный, с лёгким обоснованным мнением, без категоричности. Пиши вводку как живой человек: без подписей вроде «Главное:» или «Взгляд редактора:», без лишних двоеточий и без длинных тире.
2. Если новость продолжает сюжет из прошлых выпусков (см. блок ранее опубликованного выше), отметь это естественно словами вроде «вчера уже писали об этом», «продолжение истории про…».
3. Дедуплицируй: одну и ту же новость часто постят несколько каналов — объедини в один пункт, ссылки на все источники перечисли в конце пункта.
4. Сгруппируй новости по темам. Используй только реально наполненные группы из списка: 🚀 Релизы моделей и продуктов, 🔬 Исследования и статьи, 💼 Бизнес и индустрия, ⚖️ Регулирование и политика, 🛠 Инструменты и open source, 📰 Прочее.
5. По каждой новости: жирный мини-заголовок, затем 1–2 предложения сути; где это уместно, вплетай в текст, почему новость важна (естественно, а не отдельным ярлыком «Почему важно:»). Затем ссылки.
6. Отбрасывай рекламу, анонсы вебинаров каналов, мемы и посты без новостной ценности.
7. Пиши по-русски, живым грамотным языком, сжато, без воды. Не выдумывай факты, которых нет в постах.

Формат — HTML для Telegram (только теги <b>, <i>, <a href="...">):

<b>🤖 ИИ-дайджест за {date_human}</b>

Связная вводка на 2–4 предложения обычным текстом, без заголовков и подписей.

<b>🚀 Релизы моделей и продуктов</b>

<b>Название новости.</b> Суть в 1–2 предложениях, при необходимости с пояснением, почему это важно. <a href="ССЫЛКА">Источник</a>

(и так далее по группам)

В конце строка: <i>Всего обработано {n_posts} постов из {n_channels} каналов.</i>

Если пунктов больше ~25 — оставь только самые значимые.

Посты:
{posts_json}
"""


RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}

# Диагноз по полю error.status, а НЕ по HTTP-коду: Gemini на протухший ключ
# отвечает 400 (INVALID_ARGUMENT), а не 401, так что проверка по коду промахнётся.
LLM_DIAGNOSIS = {
    "UNAUTHENTICATED": ("key", "Ключ не принят — недействителен или отозван."),
    "PERMISSION_DENIED": ("billing", "Доступ запрещён: отключён биллинг либо у ключа нет прав на эту модель."),
    "RESOURCE_EXHAUSTED": ("quota", "Исчерпана квота или закончились средства на аккаунте."),
    "NOT_FOUND": ("model", "Модель недоступна для этого ключа — проверьте LLM_MODEL."),
}


def _parse_api_error(resp) -> tuple[str, str]:
    """Достаёт (status, message) из тела ошибки.

    Gemini заворачивает ошибку в JSON-массив — `[{"error": {...}}]`, — поэтому
    наивный resp.json()["error"] падает с TypeError.
    """
    try:
        body = resp.json()
        if isinstance(body, list):
            body = body[0] if body else {}
        err = body.get("error", {}) if isinstance(body, dict) else {}
        return str(err.get("status", "")), str(err.get("message", ""))
    except Exception:
        return "", resp.text[:300]


def _classify_llm_error(resp) -> LLMError:
    status, message = _parse_api_error(resp)
    kind, summary = LLM_DIAGNOSIS.get(status, ("", ""))
    # INVALID_ARGUMENT прилетает и на битый ключ, и на кривой запрос —
    # различаем по тексту сообщения.
    if not kind and status == "INVALID_ARGUMENT":
        if "api key" in message.lower():
            kind, summary = "key", "Ключ недействителен или отозван."
        else:
            kind, summary = "request", "Провайдер отверг запрос."
    if not kind:
        kind = f"http{resp.status_code}"
        summary = f"Провайдер вернул HTTP {resp.status_code}."
    return LLMError(summary, kind=kind,
                    detail=f"HTTP {resp.status_code} {status}\n{message or resp.text[:300]}")


def _run_llm(prompt: str, model: str | None = None) -> str:
    """Один запрос к OpenAI-совместимому /chat/completions; чистит и валидирует ответ.

    Ретраи здесь свои: у публичных
    HTTP-эндпоинтов 429/503 прилетают заметно чаще, а запуск раз в сутки по cron
    некому перезапустить вручную.
    """
    name = (model or LLM_MODEL).removeprefix("models/")
    payload = {
        "model": name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": LLM_MAX_TOKENS,
        "stream": False,
    }
    # Gemini 3.x «думает» перед ответом, и размышления тратят тот же бюджет
    # max_tokens. Задача чисто редакторская, поэтому глубина размышлений режется
    # до минимума — иначе дайджест рискует оборваться на середине HTML.
    if LLM_REASONING_EFFORT:
        payload["reasoning_effort"] = LLM_REASONING_EFFORT

    last_err = None
    last_status = None
    for attempt in range(4):
        if attempt:
            time.sleep(5 * 2 ** (attempt - 1))
        try:
            r = httpx.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json=payload,
                timeout=LLM_TIMEOUT,
            )
        except httpx.RequestError as e:
            last_err = f"сетевая ошибка: {e!r}"
            continue

        if r.status_code in RETRYABLE_STATUS:
            last_status = r.status_code
            last_err = f"HTTP {r.status_code}: {r.text[:300]}"
            continue
        if r.status_code >= 400:
            # 400/403/404 повтором не лечатся — падаем сразу с разобранным диагнозом
            raise _classify_llm_error(r)

        choice = r.json()["choices"][0]
        # finish_reason=length означает обрыв на полуслове: HTML будет битым,
        # а Telegram опубликует его как есть. Лучше упасть, чем запостить огрызок.
        if choice.get("finish_reason") == "length":
            raise LLMError(
                f"Ответ обрезан по лимиту в {LLM_MAX_TOKENS} токенов — "
                f"поднимите LLM_MAX_TOKENS.", kind="truncated")
        text = (choice["message"].get("content") or "").strip()
        if not text:
            last_err = f"пустой ответ (finish_reason={choice.get('finish_reason')})"
            continue
        # на случай, если модель всё же обернула ответ в ```
        return re.sub(r"^```(?:html|markdown)?\s*|\s*```$", "", text).strip()

    # 429, переживший все ретраи, — это почти наверняка не всплеск нагрузки,
    # а исчерпанная квота: минутный лимит за полминуты пауз успел бы отпустить.
    if last_status == 429:
        raise LLMError("Исчерпана квота или закончились средства на аккаунте "
                       "(429 не отпустил за 4 попытки).",
                       kind="quota", detail=str(last_err))
    raise LLMError(f"Провайдер недоступен: 4 попытки подряд без успеха.",
                   kind="unavailable", detail=str(last_err))


def summarize_with_llm(posts: list[dict], date_human: str,
                       previous: list[tuple] | None = None) -> str:
    """Дневной дайджест: суммаризирует посты за день."""
    prompt = PROMPT_TEMPLATE.format(
        date_human=date_human,
        n_posts=len(posts),
        n_channels=len({p["channel"] for p in posts}),
        history_block=build_history_block(previous or []),
        posts_json=json.dumps(posts, ensure_ascii=False, indent=1),
    )
    return _run_llm(prompt)


WEEKLY_PROMPT_TEMPLATE = """Ты — редактор еженедельного обзора новостей об ИИ для Telegram-канала.
Ниже — уже опубликованные ежедневные дайджесты за прошедшую неделю ({week_human}).
На их основе собери ОБЗОР КЛЮЧЕВЫХ НОВОСТЕЙ НЕДЕЛИ.

Верни ГОТОВЫЙ ТЕКСТ и ничего больше (без преамбул, без markdown-заборов):

1. Начни обзор с живой вводки на 2–4 предложения обычным связным текстом: какой была главная линия недели, что оказалось важнее всего, какие сюжеты развивались. Тон взвешенный, с лёгким обоснованным мнением, без категоричности. Пиши как живой человек: без подписей вроде «Главное:» или «Взгляд редактора:», без лишних двоеточий и без длинных тире.
2. Отбери только по-настоящему значимое за неделю — не пересказывай всё подряд (ориентир: 8–15 пунктов).
3. Объединяй связанные события недели в один пункт, показывай развитие сюжета за неделю.
4. Сгруппируй по темам: 🚀 Релизы моделей и продуктов, 🔬 Исследования, 💼 Бизнес и индустрия, ⚖️ Регулирование, 🛠 Инструменты и open source. Используй только наполненные группы.
5. По каждому пункту: жирный мини-заголовок, 1–2 предложения сути, ссылки-источники (бери их из дайджестов).
6. Пиши по-русски, живым грамотным языком, сжато. Не выдумывай фактов, которых нет в дайджестах.

Формат — HTML для Telegram (только теги <b>, <i>, <a href="...">):

<b>📅 Итоги недели в ИИ ({week_human})</b>

Связная вводка на 2–4 предложения обычным текстом, без заголовков и подписей.

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
    return _run_llm(prompt)


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


def post_to_channel(text: str, disable_preview: bool = True) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chunk in split_for_telegram(text):
        r = httpx.post(url, json={
            "chat_id": TARGET_CHANNEL,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
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
                "disable_web_page_preview": disable_preview,
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

    print("[info] суммаризирую через LLM…")
    digest = summarize_with_llm(posts, date_human, previous)

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
        with alert_on_failure("недельный обзор"):
            run_weekly(args)
    else:
        with alert_on_failure("ежедневный дайджест"):
            run_daily(args)


if __name__ == "__main__":
    main()
