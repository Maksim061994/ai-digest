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
ADMINS = {int(x) for x in re.findall(r"\d+", os.environ.get("BOT_ADMINS", ""))}
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Публичный username Telegram: буквы/цифры/подчёркивание, 4–32 символа.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")

HELP = (
    "Я управляю списком каналов-источников для ИИ-дайджеста.\n\n"
    "Команды:\n"
    "/list — показать текущие каналы\n"
    "/add @channel [ещё @channel/ссылки] — добавить\n"
    "/remove @channel [...] — удалить\n"
    "/help — эта справка\n\n"
    "Принимаю @username, username и ссылки вида https://t.me/username.\n"
    "Изменения применяются при следующем запуске дайджеста."
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
