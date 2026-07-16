#!/usr/bin/env python3
"""
Telegram-бот управления списком каналов-источников (channels.txt).

Постоянно работающий процесс: слушает команды через long-polling (getUpdates)
и правит channels.txt, который затем читает digest.py при следующем запуске.
Перезапускать дайджест не нужно — он перечитывает файл на каждом прогоне.

Команды (только для админов из BOT_ADMINS):
  /list                    — показать текущие каналы
  /add <@ch|ссылка> [...]  — добавить один или несколько каналов
  /remove <@ch> [...]      — удалить каналы
  /help                    — помощь

BOT_ADMINS в .env — id пользователей через запятую. Свой id бот покажет в ответе.
"""
import os
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
CHANNELS_FILE = BASE_DIR / "channels.txt"
SCHEDULE_FILE = BASE_DIR / "schedule.conf"
ADMINS = {int(x) for x in re.findall(r"\d+", os.environ.get("BOT_ADMINS", ""))}
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Публичный username Telegram: буквы/цифры/подчёркивание, 4–32 символа.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")
# Время HH:MM (00:00–23:59), допускаем без ведущего нуля в часах.
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

# Значения по умолчанию должны совпадать с entrypoint.sh.
SCHEDULE_DEFAULTS = {"RUN_AT": "08:00", "PAPER_AT": "13:00",
                     "WEEKLY_AT": "21:00", "WEEKLY_DOW": "7"}
SCHEDULE_ORDER = ("RUN_AT", "PAPER_AT", "WEEKLY_AT", "WEEKLY_DOW")
JOB_KEYS = {"digest": "RUN_AT", "paper": "PAPER_AT", "weekly": "WEEKLY_AT"}
WEEKDAYS = {"1": "Пн", "2": "Вт", "3": "Ср", "4": "Чт", "5": "Пт", "6": "Сб", "7": "Вс"}

HELP = (
    "Я управляю ИИ-дайджестом: список каналов и расписание публикаций.\n\n"
    "Каналы:\n"
    "/list — показать текущие каналы\n"
    "/add @channel [ещё @channel/ссылки] — добавить\n"
    "/remove @channel [...] — удалить\n\n"
    "Расписание (время по таймзоне сервера):\n"
    "/times — показать текущее расписание\n"
    "/set digest HH:MM — время ежедневного дайджеста\n"
    "/set paper HH:MM — время научного разбора\n"
    "/set weekly HH:MM — время недельного обзора\n"
    "/set weekday N — день недельного обзора (1=Пн … 7=Вс)\n\n"
    "/help — эта справка\n\n"
    "Каналы принимаю как @username, username или ссылку https://t.me/username.\n"
    "Правки каналов применяются при следующем запуске дайджеста, времени — в течение минуты."
)


# ------------------------------------------------------------- channels.txt


def normalize(token: str):
    """Приводит @username / username / t.me-ссылку к чистому username или None."""
    token = token.strip()
    m = re.search(r"(?:t\.me|telegram\.me)/(?:s/)?(@?\w+)", token)
    if m:
        token = m.group(1)
    token = token.lstrip("@").split("/")[0]
    return token if USERNAME_RE.match(token) else None


def read_file():
    """Возвращает (header_lines, entries): ведущие комментарии и список username."""
    if not CHANNELS_FILE.exists():
        return [], []
    lines = CHANNELS_FILE.read_text(encoding="utf-8").splitlines()
    header, i = [], 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        header.append(lines[i])
        i += 1
    entries = [ln.strip().lstrip("@") for ln in lines[i:]
               if ln.strip() and not ln.strip().startswith("#")]
    return header, entries


def write_file(header, entries):
    """Атомарно перезаписывает channels.txt (header + по одному username на строку)."""
    body = "\n".join(header).rstrip("\n")
    body = (body + "\n" if body else "") + "\n".join(entries) + "\n"
    tmp = CHANNELS_FILE.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(CHANNELS_FILE)


def add_channels(tokens):
    header, entries = read_file()
    seen = {e.lower() for e in entries}
    added, skipped, invalid = [], [], []
    for t in tokens:
        u = normalize(t)
        if not u:
            invalid.append(t)
        elif u.lower() in seen:
            skipped.append(u)
        else:
            entries.append(u)
            seen.add(u.lower())
            added.append(u)
    if added:
        write_file(header, entries)
    return added, skipped, invalid


def remove_channels(tokens):
    header, entries = read_file()
    targets = {normalize(t).lower() for t in tokens if normalize(t)}
    removed = [e for e in entries if e.lower() in targets]
    if removed:
        write_file(header, [e for e in entries if e.lower() not in targets])
    return removed


# ------------------------------------------------------------ расписание


def read_schedule() -> dict:
    conf = dict(SCHEDULE_DEFAULTS)
    if SCHEDULE_FILE.exists():
        for line in SCHEDULE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() in conf:
                    conf[k.strip()] = v.strip()
    return conf


def write_schedule(conf: dict) -> None:
    body = "".join(f"{k}={conf[k]}\n" for k in SCHEDULE_ORDER)
    tmp = SCHEDULE_FILE.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(SCHEDULE_FILE)


def normalize_time(s: str):
    if not TIME_RE.match(s):
        return None
    h, m = s.split(":")
    return f"{int(h):02d}:{int(m):02d}"


def handle_set(chat_id, args):
    if len(args) != 2:
        send(chat_id, "Использование: /set digest|paper|weekly HH:MM  или  /set weekday 1-7")
        return
    what, value = args[0].lower(), args[1]
    conf = read_schedule()
    if what in JOB_KEYS:
        t = normalize_time(value)
        if not t:
            send(chat_id, "Время в формате HH:MM, например 08:30.")
            return
        conf[JOB_KEYS[what]] = t
        write_schedule(conf)
        send(chat_id, f"Готово. Время «{what}» теперь {t}. Применится в течение минуты.")
    elif what in ("weekday", "dow"):
        if value not in WEEKDAYS:
            send(chat_id, "День недели числом 1–7 (1=Пн … 7=Вс).")
            return
        conf["WEEKLY_DOW"] = value
        write_schedule(conf)
        send(chat_id, f"Готово. Недельный обзор теперь по {WEEKDAYS[value]}. "
                      f"Применится в течение минуты.")
    else:
        send(chat_id, "Не понял, что менять. /set digest|paper|weekly HH:MM  или  /set weekday N")


# -------------------------------------------------------------- Telegram I/O


def send(chat_id, text):
    try:
        httpx.post(f"{API}/sendMessage",
                   json={"chat_id": chat_id, "text": text,
                         "disable_web_page_preview": True},
                   timeout=30)
    except Exception as e:
        print(f"[bot] sendMessage error: {e}", file=sys.stderr)


def handle(msg):
    chat_id = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not text or uid is None:
        return

    if not ADMINS:
        send(chat_id, f"Бот не настроен. Добавьте в .env строку BOT_ADMINS={uid} "
                      f"и перезапустите бота.")
        return
    if uid not in ADMINS:
        send(chat_id, f"⛔ Нет доступа. Ваш id: {uid}")
        return

    parts = text.split()
    cmd = parts[0].lower().split("@")[0]   # /add@my_bot -> /add
    args = parts[1:]

    if cmd in ("/start", "/help"):
        send(chat_id, HELP)
    elif cmd == "/list":
        _, entries = read_file()
        listing = "\n".join("@" + e for e in entries) or "— список пуст —"
        send(chat_id, f"Каналов: {len(entries)}\n{listing}")
    elif cmd == "/add":
        if not args:
            send(chat_id, "Использование: /add @channel [ещё @channel/ссылки]")
            return
        added, skipped, invalid = add_channels(args)
        out = []
        if added:
            out.append("✅ Добавлены: " + ", ".join("@" + u for u in added))
        if skipped:
            out.append("ℹ️ Уже были: " + ", ".join("@" + u for u in skipped))
        if invalid:
            out.append("⚠️ Не распознаны: " + ", ".join(invalid))
        if added:
            out.append(f"\nВсего каналов: {len(read_file()[1])}. "
                       f"Изменения применятся при следующем запуске дайджеста.")
        send(chat_id, "\n".join(out))
    elif cmd == "/remove":
        if not args:
            send(chat_id, "Использование: /remove @channel [...]")
            return
        removed = remove_channels(args)
        if removed:
            send(chat_id, "🗑 Удалены: " + ", ".join("@" + u for u in removed) +
                          f"\nВсего каналов: {len(read_file()[1])}.")
        else:
            send(chat_id, "Ничего не удалено — таких каналов нет в списке. /list")
    elif cmd == "/times":
        c = read_schedule()
        send(chat_id,
             "Расписание (время по таймзоне сервера):\n"
             f"Дайджест: {c['RUN_AT']}\n"
             f"Разбор статьи: {c['PAPER_AT']}\n"
             f"Недельный обзор: {c['WEEKLY_AT']}, день {c['WEEKLY_DOW']} "
             f"({WEEKDAYS.get(c['WEEKLY_DOW'], '?')})\n\n"
             "Изменить: /set digest|paper|weekly HH:MM  или  /set weekday 1-7")
    elif cmd == "/set":
        handle_set(chat_id, args)
    else:
        send(chat_id, "Неизвестная команда. /help")


def main():
    # На всякий случай снимаем webhook, иначе getUpdates вернёт 409.
    try:
        httpx.post(f"{API}/deleteWebhook", timeout=30)
    except Exception as e:
        print(f"[bot] deleteWebhook error: {e}", file=sys.stderr)

    print(f"[bot] запущен. Админы: {sorted(ADMINS) or 'НЕ ЗАДАНЫ (BOT_ADMINS)'}")
    offset = None
    while True:
        params = {"timeout": 30}
        if offset is not None:
            params["offset"] = offset
        try:
            r = httpx.get(f"{API}/getUpdates", params=params, timeout=40)
            data = r.json()
        except Exception as e:
            print(f"[bot] getUpdates error: {e}", file=sys.stderr)
            time.sleep(3)
            continue
        if not data.get("ok"):
            print(f"[bot] getUpdates не ok: {data}", file=sys.stderr)
            time.sleep(3)
            continue
        for upd in data["result"]:
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if msg:
                try:
                    handle(msg)
                except Exception as e:
                    print(f"[bot] handle error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
