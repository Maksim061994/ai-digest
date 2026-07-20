#!/usr/bin/env python3
"""
Ежедневный научный разбор одной статьи (по расписанию — в 13:00).

Пайплайн:
  1. Собираем посты каналов за последние PAPER_LOOKBACK_DAYS дней (те же источники,
     что и дайджест) и вытаскиваем из них arXiv-идентификаторы.
  2. Исключаем уже разобранные (paper_history.txt); если кандидатов несколько —
     Claude выбирает самую значимую статью.
  3. Скачиваем PDF с arXiv, извлекаем текст (PyMuPDF) и метаданные (arXiv API).
  4. Claude пишет строгий, но понятный разбор в стиле д.т.н. (без смайликов, со
     ссылкой на статью, с пояснением терминов, структурой идея/метод/результат/применение).
  5. Best-effort достаём из PDF ключевую иллюстрацию (крупное растровое изображение)
     и публикуем пост картинкой + текстом. Векторные фигуры не извлекаются — тогда
     пост выходит текстовым.

Запуск: python paper.py               — разбор за последние дни
        python paper.py --dry-run     — не постить, вывести в stdout
        python paper.py --id 2401.12345 — принудительно разобрать конкретную статью
"""
import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import httpx

import digest  # переиспользуем сбор постов, вызов claude и публикацию

TIMEZONE = digest.TIMEZONE
TARGET_CHANNEL = digest.TARGET_CHANNEL
BOT_TOKEN = digest.BOT_TOKEN
BASE_DIR = digest.BASE_DIR

PAPER_MODEL = os.environ.get("PAPER_MODEL", os.environ.get("CLAUDE_MODEL", "sonnet"))
PAPER_LOOKBACK_DAYS = int(os.environ.get("PAPER_LOOKBACK_DAYS", "3"))
PAPER_HISTORY = BASE_DIR / "paper_history.txt"
PAPER_TEXT_BUDGET = 45000       # сколько символов текста статьи класть в промпт
CAPTION_LIMIT = 1024            # лимит подписи к фото в Telegram
# Извлекать иллюстрацию из PDF. На тесных по памяти серверах можно выключить (PAPER_FIGURE=0).
PAPER_FIGURE = os.environ.get("PAPER_FIGURE", "1") not in ("0", "false", "no", "")

# arXiv id в ссылке (abs/pdf) или в форме "arXiv:2401.12345"
ARXIV_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:\s*)(\d{4}\.\d{4,5})", re.I)


# ------------------------------------------------------------- выбор статьи


def extract_candidates(posts: list[dict]) -> dict:
    """id статьи -> пример поста, где она упомянута (для контекста при выборе)."""
    found = {}
    for p in posts:
        for m in ARXIV_RE.finditer(p.get("text", "")):
            found.setdefault(m.group(1), p)
    return found


def load_reviewed() -> set:
    if PAPER_HISTORY.exists():
        return {ln.strip() for ln in PAPER_HISTORY.read_text(encoding="utf-8").splitlines()
                if ln.strip()}
    return set()


def mark_reviewed(arxiv_id: str) -> None:
    with PAPER_HISTORY.open("a", encoding="utf-8") as f:
        f.write(arxiv_id + "\n")


SELECT_PROMPT = """Ты — научный редактор. Ниже список статей (arXiv), упомянутых в новостях за последние дни, с фрагментами постов. Выбери ОДНУ — самую значимую и интересную для глубокого разбора (приоритет: новизна, влияние на область, содержательность). Ответь ТОЛЬКО идентификатором arXiv выбранной статьи (например, 2401.12345), без пояснений.

Кандидаты:
"""


def choose_paper(candidates: dict) -> str:
    ids = list(candidates)
    if len(ids) == 1:
        return ids[0]
    listing = "\n".join(f"- arXiv:{aid} — {candidates[aid]['text'][:400]}" for aid in ids)
    answer = digest._run_claude(SELECT_PROMPT + listing, PAPER_MODEL)
    m = re.search(r"\d{4}\.\d{4,5}", answer)
    return m.group(0) if (m and m.group(0) in candidates) else ids[0]


# --------------------------------------------------------- загрузка статьи


def fetch_metadata(arxiv_id: str) -> dict:
    """Название, авторы, аннотация с arXiv API."""
    r = httpx.get("https://export.arxiv.org/api/query",
                  params={"id_list": arxiv_id}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = ET.fromstring(r.text).find("a:entry", ns)
    if entry is None:
        return {"title": "", "authors": [], "summary": ""}
    title = " ".join((entry.findtext("a:title", "", ns) or "").split())
    summary = " ".join((entry.findtext("a:summary", "", ns) or "").split())
    authors = [(a.findtext("a:name", "", ns) or "").strip()
               for a in entry.findall("a:author", ns)]
    return {"title": title, "authors": authors, "summary": summary}


def fetch_pdf(arxiv_id: str) -> bytes:
    r = httpx.get(f"https://arxiv.org/pdf/{arxiv_id}", timeout=90, follow_redirects=True)
    r.raise_for_status()
    return r.content


def extract_text_and_figure(pdf_bytes: bytes):
    """Возвращает (полный_текст, figure|None). figure = (bytes, ext, w, h)."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    texts, candidates = [], []
    for pno, page in enumerate(doc):
        texts.append(page.get_text())
        if not PAPER_FIGURE:
            continue
        # Только лёгкие метаданные (размер из get_images, без декодирования картинок),
        # иначе держим в памяти все изображения статьи разом -> OOM.
        for img in page.get_images(full=True):
            xref, w, h = img[0], img[2], img[3]
            if w >= 400 and h >= 250:
                candidates.append((pno, w * h, xref))

    figure = None
    if candidates:
        # предпочитаем крупнейшее изображение НЕ с титульной страницы;
        # байты достаём только у выбранного.
        pool = [c for c in candidates if c[0] > 0] or candidates
        _, _, xref = max(pool, key=lambda c: c[1])
        try:
            info = doc.extract_image(xref)
            if info.get("ext") in ("png", "jpg", "jpeg"):
                figure = (info["image"], info["ext"],
                          info.get("width", 0), info.get("height", 0))
        except Exception:
            figure = None
    return "\n".join(texts), figure


# ----------------------------------------------------------------- разбор


REVIEW_PROMPT_TEMPLATE = """Ты — доктор технических наук, который ведёт научно-популярный Telegram-канал и делает глубокие разборы свежих статей по ИИ и смежным областям. Разбери статью ниже.

Требования к тексту:
- Стиль строгий и научный, но понятный образованному читателю без узкой специализации. Где вводишь термин — поясни его кратко в скобках или одним предложением.
- Говори по сути: в чём главная идея и мотивация; что конкретно предложили; как это проверяли (данные, эксперименты, метрики, с чем сравнивали); какие получены результаты и насколько они убедительны; где это применимо и какие ограничения.
- Не выдумывай фактов, которых нет в статье. Если чего-то нет — не пиши об этом.
- Без смайликов и эмодзи. Без маркетинговых восклицаний.
- Пиши грамотным, естественным, живым русским языком — так, как написал бы человек, а не сгенерированный текст. Связные предложения вместо телеграфного перечисления.
- Избегай лишних двоеточий: ставь двоеточие только там, где оно действительно нужно (перед настоящим перечислением или прямым пояснением). Не начинай каждый абзац с «Идея:», «Метод:» и т. п.
- Избегай длинных тире (—) как стилистического приёма: перестраивай фразы обычными знаками препинания.
- Объём — примерно 2500–4500 символов.

Формат — HTML для Telegram (только теги <b>, <i>, <a href="...">), без markdown-заборов:

<b>Заголовок разбора</b>

<i>Авторы, где уместно; ссылка на статью: <a href="{abs_url}">arXiv:{arxiv_id}</a></i>

Далее связный текст с подзаголовками через <b>…</b> по смысловым блокам (Идея, Метод, Как проверяли, Результаты, Применение и ограничения). В конце обязательно оставь ссылку <a href="{abs_url}">{abs_url}</a>.

Метаданные:
Название: {title}
Авторы: {authors}
Аннотация: {summary}

Текст статьи (может быть обрезан):
{body}
"""


def write_review(arxiv_id: str, meta: dict, body: str) -> str:
    abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    authors = ", ".join(meta["authors"][:8]) + (" и др." if len(meta["authors"]) > 8 else "")
    prompt = REVIEW_PROMPT_TEMPLATE.format(
        abs_url=abs_url, arxiv_id=arxiv_id, title=meta["title"],
        authors=authors or "—", summary=meta["summary"] or "—",
        body=body[:PAPER_TEXT_BUDGET],
    )
    return digest._run_claude(prompt, PAPER_MODEL)


# -------------------------------------------------------------- публикация


def _split_caption(text: str, limit: int = CAPTION_LIMIT):
    """Первая часть <= limit (по границе абзаца/строки) под подпись к фото + остаток."""
    if len(text) <= limit:
        return text, ""
    for sep in ("\n\n", "\n", " "):
        cut = text.rfind(sep, 0, limit)
        if cut > 0:
            return text[:cut].strip(), text[cut:].strip()
    return text[:limit], text[limit:]


def post_review(html_text: str, figure) -> None:
    if not figure:
        digest.post_to_channel(html_text)
        return

    data_bytes, ext, _, _ = figure
    caption, rest = _split_caption(html_text)
    r = httpx.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        data={"chat_id": TARGET_CHANNEL, "caption": caption, "parse_mode": "HTML"},
        files={"photo": (f"figure.{ext}", data_bytes)},
        timeout=90,
    )
    if not r.json().get("ok"):
        print(f"[warn] sendPhoto не удался: {r.text[:300]}. Публикую текстом.",
              file=sys.stderr)
        digest.post_to_channel(html_text)
        return
    if rest.strip():
        digest.post_to_channel(rest)


# --------------------------------------------------------------------- main


def run(args) -> None:
    if args.id:
        arxiv_id = args.id
        print(f"[info] принудительный разбор arXiv:{arxiv_id}")
    else:
        now = (datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TIMEZONE)
               if args.date else datetime.now(TIMEZONE))
        today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = today0 + timedelta(days=1)
        day_start = today0 - timedelta(days=PAPER_LOOKBACK_DAYS - 1)

        print(f"[info] ищу статьи в постах за последние {PAPER_LOOKBACK_DAYS} дн. "
              f"из {len(digest.CHANNELS)} каналов…")
        posts = digest.asyncio.run(digest.collect_posts(day_start, day_end))
        candidates = extract_candidates(posts)
        reviewed = load_reviewed()
        fresh = {k: v for k, v in candidates.items() if k not in reviewed}
        print(f"[info] найдено статей: {len(candidates)}, из них не разобранных: {len(fresh)}")

        if not fresh:
            print("[info] новых статей для разбора нет — пост не публикуется")
            return
        arxiv_id = choose_paper(fresh)

    print(f"[info] выбрана статья: arXiv:{arxiv_id}. Скачиваю PDF…")
    meta = fetch_metadata(arxiv_id)
    pdf = fetch_pdf(arxiv_id)
    body, figure = extract_text_and_figure(pdf)
    print(f"[info] текст: {len(body)} символов; иллюстрация: "
          f"{'да, %dx%d' % (figure[2], figure[3]) if figure else 'не найдена (текстовый пост)'}")

    if len(body) < 500:
        print("[warn] не удалось извлечь текст статьи — пропускаю", file=sys.stderr)
        return

    print(f"[info] пишу разбор через claude -p (модель {PAPER_MODEL})…")
    review = write_review(arxiv_id, meta, body)

    if args.dry_run:
        if figure:
            preview = BASE_DIR / f"paper_preview.{figure[1]}"
            preview.write_bytes(figure[0])
            print(f"[info] превью иллюстрации сохранено: {preview}")
        print("\n" + "=" * 60 + "\n" + review)
        return

    print("[info] публикую разбор…")
    post_review(review, figure)
    mark_reviewed(arxiv_id)
    print("[info] готово ✅")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="не постить, вывести разбор в stdout")
    parser.add_argument("--date", help="дата отсчёта окна поиска YYYY-MM-DD (по умолчанию сегодня)")
    parser.add_argument("--id", help="принудительно разобрать конкретный arXiv id")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
