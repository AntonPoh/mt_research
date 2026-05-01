"""
Telegram Trade Monitor — Telethon версия для Railway
=====================================================
Читает сообщения из торговых групп от имени пользователя (Telethon).
Считает суммарный ROE % по монетам за 30 минут.
Шлёт алерт в группу когда монета набирает >= 1.5%.
"""

import os
import asyncio
import re
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ─────────────────────────────────────────
#  НАСТРОЙКИ — переменные окружения Railway
# ─────────────────────────────────────────

API_ID         = int(os.environ.get("API_ID", "0"))
API_HASH       = os.environ.get("API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ALERT_GROUP_ID = int(os.environ.get("ALERT_GROUP_ID", "0"))
ALERT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", "1.5"))
WINDOW_MINUTES  = int(os.environ.get("WINDOW_MINUTES", "30"))
COOLDOWN_MINUTES = int(os.environ.get("COOLDOWN_MINUTES", "30"))

# ID торговых групп (через запятую в переменной окружения)
# Пример: GROUPS=-1002422661504,-1003600846314,...
GROUPS_ENV = os.environ.get("GROUPS", "")
GROUPS = [int(g.strip()) for g in GROUPS_ENV.split(",") if g.strip()]

# ─────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
#  ПАРСИНГ
# ─────────────────────────────────────────

CLOSE_RE = re.compile(
    r"(?:Profit|Loss)"
    r"\s+[+-]?[\d.]+\$?"
    r"\s+\(([+-]?[\d.]+)%\)",
    re.IGNORECASE,
)
SYMBOL_RE  = re.compile(r"#(\w+)", re.IGNORECASE)
ACCOUNT_RE = re.compile(r"^([^,]+),")


def parse_lines(text: str):
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = CLOSE_RE.search(line)
        if not m:
            continue
        acc_m   = ACCOUNT_RE.match(line)
        account = acc_m.group(1).strip() if acc_m else "unknown"
        sym_m   = SYMBOL_RE.search(line)
        if not sym_m:
            continue
        symbol = sym_m.group(1).upper()
        pct    = float(m.group(1))
        results.append((account, symbol, pct))
    return results


# ─────────────────────────────────────────
#  СОСТОЯНИЕ
# ─────────────────────────────────────────

trades  = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
alerted = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: datetime.min)))

# ─────────────────────────────────────────
#  ОТПРАВКА АЛЕРТОВ через Bot API
# ─────────────────────────────────────────

import aiohttp

async def send_alert(session: aiohttp.ClientSession, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ALERT_GROUP_ID, "text": text, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error(f"Ошибка отправки: {resp.status} {body}")
    except Exception as e:
        log.error(f"send_alert error: {e}")


def format_alert(account, symbol, total_pct, group_title, records):
    sign   = "+" if total_pct >= 0 else ""
    now    = datetime.utcnow()
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)
    recent = [(ts, p) for ts, p in records if ts >= cutoff]
    lines  = [
        f"🚨 *Алерт по монете*", f"",
        f"📊 *Аккаунт:* `{account}`",
        f"📣 *Группа:* {group_title}",
        f"💎 *Монета:* #{symbol}",
        f"📈 *Суммарный ROE за {WINDOW_MINUTES} мин:* `{sign}{total_pct:.2f}%`",
        f"", f"*Сделки за окно:*",
    ]
    for ts, p in recent[-10:]:
        s = "+" if p >= 0 else ""
        lines.append(f"  `{ts.strftime('%H:%M')}` → `{s}{p:.1f}%`")
    return "\n".join(lines)


# ─────────────────────────────────────────
#  ОСНОВНОЙ КОД
# ─────────────────────────────────────────

async def main():
    http_session = aiohttp.ClientSession()

    client = TelegramClient(
        StringSession(SESSION_STRING), API_ID, API_HASH
    )

    await client.start()
    log.info("✅ Клиент запущен")

    # Подключаем группы
    for group_id in GROUPS:
        try:
            entity = await client.get_entity(group_id)
            title  = getattr(entity, "title", str(group_id))
            log.info(f"  📡 Подключено: {title} (id={group_id})")
        except Exception as e:
            log.error(f"  ❌ Не удалось подключить {group_id}: {e}")

    @client.on(events.NewMessage(chats=GROUPS))
    async def handler(event):
        text        = event.message.text or ""
        group_title = getattr(event.chat, "title", str(event.chat_id))

        if not text:
            return

        parsed = parse_lines(text)
        if not parsed:
            return

        now = datetime.utcnow()
        chat_id = event.chat_id

        for account, symbol, pct in parsed:
            trades[chat_id][account][symbol].append((now, pct))

            cutoff2 = now - timedelta(minutes=WINDOW_MINUTES * 2)
            trades[chat_id][account][symbol] = [
                (ts, p) for ts, p in trades[chat_id][account][symbol] if ts >= cutoff2
            ]

            cutoff = now - timedelta(minutes=WINDOW_MINUTES)
            total  = sum(p for ts, p in trades[chat_id][account][symbol] if ts >= cutoff)

            log.info(f"[{group_title}] {account} | {symbol} {pct:+.1f}% | окно: {total:+.2f}%")

            if total < ALERT_THRESHOLD:
                continue

            last = alerted[chat_id][account][symbol]
            if now - last < timedelta(minutes=COOLDOWN_MINUTES):
                log.info(f"  → кулдаун {symbol}, пропускаем")
                continue

            alerted[chat_id][account][symbol] = now
            text_alert = format_alert(account, symbol, total, group_title,
                                       trades[chat_id][account][symbol])
            log.info(f"  → 🚨 АЛЕРТ: {symbol} {total:+.2f}% [{account}] [{group_title}]")
            await send_alert(http_session, text_alert)

    log.info(f"🔍 Мониторинг запущен | порог {ALERT_THRESHOLD}% за {WINDOW_MINUTES} мин")
    await client.run_until_disconnected()
    await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
