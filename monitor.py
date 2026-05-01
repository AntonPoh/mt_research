"""
Telegram Trade Monitor — Telethon версия для Railway
"""

import os
import asyncio
import re
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import aiohttp

API_ID           = int(os.environ.get("API_ID", "0"))
API_HASH         = os.environ.get("API_HASH", "")
SESSION_STRING   = os.environ.get("SESSION_STRING", "")
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
ALERT_GROUP_ID   = int(os.environ.get("ALERT_GROUP_ID", "0"))
ALERT_THRESHOLD  = float(os.environ.get("ALERT_THRESHOLD", "1.5"))
WINDOW_MINUTES   = int(os.environ.get("WINDOW_MINUTES", "30"))
COOLDOWN_MINUTES = int(os.environ.get("COOLDOWN_MINUTES", "30"))

# Названия групп через запятую
# Пример: GROUPS_NAMES=BBT_1,BBT_2,BBT_3,BIN_1,BIN_2
GROUPS_NAMES_ENV = os.environ.get("GROUPS_NAMES", "")
GROUPS_NAMES = [g.strip() for g in GROUPS_NAMES_ENV.split(",") if g.strip()]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

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


trades  = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
alerted = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: datetime.min)))


async def send_alert(http: aiohttp.ClientSession, text: str):
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ALERT_GROUP_ID, "text": text, "parse_mode": "Markdown"}
    try:
        async with http.post(url, json=payload) as resp:
            if resp.status != 200:
                log.error(f"Ошибка отправки: {resp.status} {await resp.text()}")
    except Exception as e:
        log.error(f"send_alert error: {e}")


def format_alert(account, symbol, total_pct, group_title, records):
    sign   = "+" if total_pct >= 0 else ""
    cutoff = datetime.utcnow() - timedelta(minutes=WINDOW_MINUTES)
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


async def main():
    http   = aiohttp.ClientSession()
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()
    log.info("✅ Клиент запущен")

    # Находим группы по названию
    group_ids = []
    async for dialog in client.iter_dialogs():
        if dialog.name in GROUPS_NAMES:
            group_ids.append(dialog.id)
            log.info(f"  📡 Найдена группа: {dialog.name} (id={dialog.id})")

    if not group_ids:
        log.error("❌ Ни одна группа не найдена! Проверь GROUPS_NAMES")
        return

    log.info(f"✅ Подключено групп: {len(group_ids)}")

    @client.on(events.NewMessage(chats=group_ids))
    async def handler(event):
        text        = event.message.text or ""
        group_title = getattr(event.chat, "title", str(event.chat_id))

        if not text:
            return

        parsed = parse_lines(text)
        if not parsed:
            return

        now     = datetime.utcnow()
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
            await send_alert(http, text_alert)

    log.info(f"🔍 Мониторинг запущен | порог {ALERT_THRESHOLD}% за {WINDOW_MINUTES} мин")
    await client.run_until_disconnected()
    await http.close()


if __name__ == "__main__":
    asyncio.run(main())
