#!/usr/bin/env python3
"""
«Живой» пост раз в пару дней вечером: случайно один из трёх форматов —
  joke   — короткая умная шутка про ИИ;
  future — размышление о том, куда движется ИИ и каким может быть будущее;
  repo   — разбор популярного GitHub-репозитория про ИИ (ищется через GitHub API).

Окно времени и «случайный вечерний запуск» задаёт планировщик entrypoint.sh.
Интервал «раз в VIBE_EVERY_DAYS дней» стережёт этот скрипт через vibe_last.txt,
поэтому планировщик может дёргать его хоть каждый день — лишнего поста не будет.

Запуск: python vibe.py                       — случайный тип, с учётом интервала
        python vibe.py --dry-run             — не постить, вывести в stdout
        python vibe.py --type joke|future|repo  — принудительный тип
        python vibe.py --force               — игнорировать интервал (для теста)
"""
import argparse
import json
import os
import random
import re
import sys
from datetime import datetime

import httpx

import digest  # переиспользуем вызов claude и публикацию

TIMEZONE = digest.TIMEZONE
BASE_DIR = digest.BASE_DIR

VIBE_MODEL = os.environ.get("VIBE_MODEL", os.environ.get("CLAUDE_MODEL", "sonnet"))
VIBE_EVERY_DAYS = int(os.environ.get("VIBE_EVERY_DAYS", "2"))
VIBE_STATE = BASE_DIR / "vibe_last.txt"      # дата последнего поста
VIBE_REPOS = BASE_DIR / "vibe_repos.txt"     # уже показанные репозитории

AI_TOPICS = ["machine-learning", "deep-learning", "artificial-intelligence",
             "llm", "large-language-models", "generative-ai", "ai-agents",
             "rag", "transformers", "computer-vision", "nlp", "diffusion-models"]


# --------------------------------------------------------------- интервал


def should_post(today) -> bool:
    if not VIBE_STATE.exists():
        return True
    try:
        last = datetime.strptime(
            VIBE_STATE.read_text(encoding="utf-8").strip(), "%Y-%m-%d").date()
    except ValueError:
        return True
    return (today - last).days >= VIBE_EVERY_DAYS


def mark_posted(today) -> None:
    VIBE_STATE.write_text(today.strftime("%Y-%m-%d"), encoding="utf-8")


# ---------------------------------------------------------------- контент


JOKE_PROMPT = """Придумай одну свежую, остроумную и умную шутку про искусственный интеллект, нейросети или жизнь рядом с ИИ. Не бородатую, не кринжовую, без клише уровня «Скайнет захватит мир». Ценятся тонкая ирония, самоирония или неожиданный поворот. 1–3 предложения, по-русски. Верни ТОЛЬКО текст шутки, без вступлений и пояснений. Без эмодзи или максимум один уместный."""

FUTURE_PROMPT = """Напиши короткий аналитический пост для Telegram-канала про ИИ: куда движется искусственный интеллект и каким может быть будущее. Возьми один чёткий тезис и разверни его строго и по существу, опираясь на наблюдаемые тенденции, а не на домыслы.

Стиль научный и строгий, но понятный: точные формулировки, ясная логика рассуждения, аккуратные выводы без категоричности. Избегай эссеистичной манеры «думаю вслух», лишнего первого лица и бытовых зарисовок. Где вводишь термин — поясни его кратко. Без хайпа, без апокалиптических клише, без банальностей и без маркетинговых восклицаний. Не выдумывай конкретных фактов, цифр и цитат.

По-русски, 500–1100 символов. Формат — HTML для Telegram (только <b>, <i>), без заголовков-ярлыков и без эмодзи."""

REPO_PROMPT_TEMPLATE = """Ниже данные о популярном GitHub-репозитории, связанном с ИИ. Напиши короткий увлекательный пост для Telegram-канала: что это такое, какую задачу решает, чем полезен или интересен, кому пригодится. Живо и по делу, без воды и без построчного пересказа README. По-русски, 500–1100 символов. Не выдумывай того, чего нет в данных. Обязательно укажи число звёзд и дай ссылку на репозиторий. Формат — HTML для Telegram (только <b>, <i>, <a href="...">), без эмодзи-перегруза.

Данные о репозитории:
{repo_json}"""


def make_joke() -> str:
    return digest._run_claude(JOKE_PROMPT, VIBE_MODEL)


def make_future() -> str:
    return digest._run_claude(FUTURE_PROMPT, VIBE_MODEL)


def load_featured() -> set:
    if VIBE_REPOS.exists():
        return {ln.strip().lower()
                for ln in VIBE_REPOS.read_text(encoding="utf-8").splitlines() if ln.strip()}
    return set()


def fetch_candidates(exclude: set) -> list:
    """Собирает пул популярных AI-репозиториев из нескольких топиков (без показанных)."""
    topics = AI_TOPICS[:]
    random.shuffle(topics)
    found = {}
    for topic in topics[:3]:
        try:
            r = httpx.get("https://api.github.com/search/repositories",
                          params={"q": f"topic:{topic}", "sort": "stars",
                                  "order": "desc", "per_page": 15},
                          headers={"Accept": "application/vnd.github+json"},
                          timeout=30)
            r.raise_for_status()
        except Exception as e:
            print(f"[warn] GitHub API ({topic}): {e}", file=sys.stderr)
            continue
        for it in r.json().get("items", []):
            key = it["full_name"].lower()
            if key not in exclude and key not in found:
                found[key] = it
    return list(found.values())


SELECT_REPO_PROMPT = """Ниже список GitHub-репозиториев про ИИ (топ по звёздам). Выбери ОДИН — самый по-настоящему известный, полезный и интересный для поста. Избегай подозрительных, явно накрученных по звёздам или малоизвестных проектов; отдавай предпочтение проектам с реальной репутацией в сообществе. Ответь ТОЛЬКО в формате owner/repo, без пояснений.

Список:
"""


def choose_repo(candidates: list) -> dict:
    if len(candidates) == 1:
        return candidates[0]
    listing = "\n".join(
        f"- {c['full_name']} (звёзд {c.get('stargazers_count', 0)}): "
        f"{(c.get('description') or '')[:150]}" for c in candidates)
    answer = digest._run_claude(SELECT_REPO_PROMPT + listing, VIBE_MODEL)
    m = re.search(r"[\w.-]+/[\w.-]+", answer)
    if m:
        pick = m.group(0).lower()
        for c in candidates:
            if c["full_name"].lower() == pick:
                return c
    return candidates[0]


def make_repo() -> tuple:
    candidates = fetch_candidates(load_featured())
    if not candidates:
        raise RuntimeError("не нашёл подходящий GitHub-репозиторий")
    repo = choose_repo(candidates)
    data = {
        "name": repo["full_name"],
        "url": repo["html_url"],
        "description": repo.get("description") or "",
        "stars": repo.get("stargazers_count", 0),
        "language": repo.get("language") or "",
        "topics": (repo.get("topics") or [])[:8],
    }
    text = digest._run_claude(
        REPO_PROMPT_TEMPLATE.format(
            repo_json=json.dumps(data, ensure_ascii=False, indent=1)),
        VIBE_MODEL)
    return text, repo["full_name"]


# ------------------------------------------------------------------- main


def run(args) -> None:
    today = datetime.now(TIMEZONE).date()
    if not args.force and not should_post(today):
        print(f"[info] с прошлого поста не прошло {VIBE_EVERY_DAYS} дн. — пропуск")
        return

    kind = args.type or random.choice(["joke", "future", "repo"])
    print(f"[info] тип поста: {kind}")

    featured_repo = None
    preview = False
    if kind == "joke":
        text = make_joke()
    elif kind == "future":
        text = make_future()
    else:
        try:
            text, featured_repo = make_repo()
            preview = True   # для репо оставляем превью-карточку GitHub
        except Exception as e:
            print(f"[warn] репо не получилось ({e}) — переключаюсь на 'future'",
                  file=sys.stderr)
            kind, text = "future", make_future()

    if args.dry_run:
        print(f"\n{'=' * 60}\n[{kind}]\n{text}")
        return

    digest.post_to_channel(text, disable_preview=not preview)
    if featured_repo:
        with VIBE_REPOS.open("a", encoding="utf-8") as f:
            f.write(featured_repo + "\n")
    if not args.force:
        mark_posted(today)
    print("[info] готово ✅")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="не постить, вывести в stdout")
    p.add_argument("--type", choices=["joke", "future", "repo"],
                   help="принудительно выбрать тип поста")
    p.add_argument("--force", action="store_true", help="игнорировать интервал")
    run(p.parse_args())


if __name__ == "__main__":
    main()
