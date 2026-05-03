"""
Telegram Trade Monitor — Telethon версия для Railway
Считает только TEST ордера, ищет прибыльные монеты для рабочего ордера.
"""

import os
import asyncio
import re
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import aiohttp

API_ID               = int(os.environ.get("API_ID", "0"))
API_HASH             = os.environ.get("API_HASH", "")
SESSION_STRING       = os.environ.get("SESSION_STRING", "")
BOT_TOKEN            = os.environ.get("BOT_TOKEN", "")
ALERT_GROUP_ID       = int(os.environ.get("ALERT_GROUP_ID", "0"))
ALERT_THRESHOLD      = float(os.environ.get("ALERT_THRESHOLD", "1.5"))
ALERT_THRESHOLD_NEG  = float(os.environ.get("ALERT_THRESHOLD_NEG", "-1.5"))
WINDOW_MINUTES       = int(os.environ.get("WINDOW_MINUTES", "1440"))
COOLDOWN_MINUTES     = int(os.environ.get("COOLDOWN_MINUTES", "120"))
GROUPS_NAMES_ENV     = os.environ.get("GROUPS_NAMES", "")
GROUPS_NAMES         = [g.strip() for g in GROUPS_NAMES_ENV.split(",") if g.strip()]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Последний % в строке — реальное движение цены без плеча
CLOSE_RE = re.compile(
    r"(?:Profit|Loss)"
    r".+?"
    r"\(([+-]?[\d.]+)%\)\s*'?\s*$",
    re.IGNORECASE,
)
SYMBOL_RE  = re.compile(r"#(\w+)", re.IGNORECASE)
ACCOUNT_RE = re.compile(r"^([^,]+),")
# Проверяем что сигнал тестовый (содержит TEST)
TEST_RE    = re.compile(r"_TEST", re.IGNORECASE)


def parse_lines(text: str):
    """
    Парсит строки сообщения.
    Возвращает только TEST сделки: (account, symbol, pct)
    """
    results = []
    for line in text.splitlines():
        line = line.strip().replace('**', '').replace('__', '')
        if not line:
            continue

        # Только тестовые сигналы
        if not TEST_RE.search(line):
            continue

        m = CLOSE_RE.search(line)
        if not m:
            continue

        acc_m   = ACCOUNT_RE.match(line)
        account = acc_m.group(1).strip() if acc_m else "unknown"

        sym_m = SYMBOL_RE.search(line)
        if not sym_m:
            continue

        symbol = sym_m.group(1).upper()
        pct    = float(m.group(1))
        results.append((account, symbol, pct))
    return results


trades  = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
alerted = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: datetime.min)))


async def send_message(http: aiohttp.ClientSession, text: str):
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ALERT_GROUP_ID, "text": text}
    try:
        async with http.post(url, json=payload) as resp:
            if resp.status != 200:
                log.error(f"Ошибка отправки: {resp.status} {await resp.text()}")
    except Exception as e:
        log.error(f"send_message error: {e}")


def format_alert(account, symbol, total_pct, group_title, records, is_positive):
    sign   = "+" if total_pct >= 0 else ""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)
    recent = [(ts, p) for ts, p in records if ts >= cutoff]

    if is_positive:
        header = "🟢 ДОБАВИТЬ НА РАБОЧИЙ ОРДЕР"
    else:
        header = "🔴 УБРАТЬ С ТЕСТОВОГО ОРДЕРА"

    # Считаем winrate
    wins   = sum(1 for _, p in recent if p > 0)
    total  = len(recent)
    wr     = round(wins / total * 100) if total > 0 else 0

    lines = [
        header, "",
        f"Аккаунт: {account}",
        f"Группа: {group_title}",
        f"Монета: #{symbol}",
        f"Суммарный % за день: {sign}{total_pct:.2f}%",
        f"Сделок за день: {total} (WR: {wr}%)",
        "", "Сделки:",
    ]
    for ts, p in recent[-15:]:
        s  = "+" if p >= 0 else ""
        emoji = "✅" if p > 0 else "❌"
        lines.append(f"  {emoji} {ts.strftime('%H:%M')} -> {s}{p:.2f}%")
    return "\n".join(lines)


def process_trade(chat_id, account, symbol, pct, ts):
    trades[chat_id][account][symbol].append((ts, pct))
    cutoff2 = ts - timedelta(minutes=WINDOW_MINUTES * 2)
    trades[chat_id][account][symbol] = [
        (t, p) for t, p in trades[chat_id][account][symbol] if t >= cutoff2
    ]
    cutoff = ts - timedelta(minutes=WINDOW_MINUTES)
    total  = sum(p for t, p in trades[chat_id][account][symbol] if t >= cutoff)
    return total


async def load_history(client, group_ids):
    """Читает историю за сегодня из всех групп."""
    now   = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total_loaded = 0

    for gid in group_ids:
        try:
            count = 0
            async for msg in client.iter_messages(gid, limit=500):
                if not msg.text:
                    continue
                msg_time = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
                if msg_time < today:
                    break
                parsed = parse_lines(msg.text)
                for account, symbol, pct in parsed:
                    process_trade(gid, account, symbol, pct, msg_time)
                    count += 1
            log.info(f"  📚 История группы id={gid}: {count} TEST сделок")
            total_loaded += count
        except Exception as e:
            log.error(f"  ❌ Ошибка загрузки истории {gid}: {e}")

    log.info(f"✅ Итого из истории: {total_loaded} TEST сделок")


async def main():
    http   = aiohttp.ClientSession()
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()
    log.info("✅ Клиент запущен")

    # Находим группы по названию (без дублей)
    group_ids   = []
    group_names = {}
    async for dialog in client.iter_dialogs():
        if dialog.name in GROUPS_NAMES and dialog.id not in group_ids:
            group_ids.append(dialog.id)
            group_names[dialog.id] = dialog.name
            log.info(f"  📡 {dialog.name} (id={dialog.id})")

    if not group_ids:
        log.error("❌ Группы не найдены!")
        return

    log.info(f"✅ Подключено групп: {len(group_ids)}")

    # Загружаем историю за сегодня
    log.info("📚 Загружаем историю за сегодня...")
    await load_history(client, group_ids)

    @client.on(events.NewMessage(chats=group_ids))
    async def handler(event):
        text        = event.message.text or ""
        group_title = getattr(event.chat, "title", str(event.chat_id))

        if not text:
            return

        parsed = parse_lines(text)
        if not parsed:
            return

        now     = datetime.now(timezone.utc)
        chat_id = event.chat_id

        for account, symbol, pct in parsed:
            total = process_trade(chat_id, account, symbol, pct, now)
            log.info(f"[{group_title}] {account} | {symbol} {pct:+.2f}% | день: {total:+.2f}%")

            last = alerted[chat_id][account][symbol]
            if now - last < timedelta(minutes=COOLDOWN_MINUTES):
                continue

            if total >= ALERT_THRESHOLD:
                alerted[chat_id][account][symbol] = now
                text_alert = format_alert(account, symbol, total, group_title,
                                          trades[chat_id][account][symbol], True)
                log.info(f"  → 🟢 ДОБАВИТЬ: {symbol} {total:+.2f}%")
                await send_message(http, text_alert)

            elif total <= ALERT_THRESHOLD_NEG:
                alerted[chat_id][account][symbol] = now
                text_alert = format_alert(account, symbol, total, group_title,
                                          trades[chat_id][account][symbol], False)
                log.info(f"  → 🔴 УБРАТЬ: {symbol} {total:+.2f}%")
                await send_message(http, text_alert)

    log.info(f"🔍 Мониторинг TEST ордеров | +{ALERT_THRESHOLD}% / {ALERT_THRESHOLD_NEG}%")
    await client.run_until_disconnected()
    await http.close()


if __name__ == "__main__":
    asyncio.run(main())
