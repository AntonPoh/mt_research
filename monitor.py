"""
Telegram Trade Monitor — Telethon версия для Railway
Считает только TEST ордера, разделяет лонг/шорт.
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

CLOSE_RE   = re.compile(r"(?:Profit|Loss).+?\(([+-]?[\d.]+)%\)\s*'?\s*$", re.IGNORECASE)
SYMBOL_RE  = re.compile(r"#(\w+)", re.IGNORECASE)
ACCOUNT_RE = re.compile(r"^([^,]+),")
TEST_RE    = re.compile(r"_TEST", re.IGNORECASE)
SIDE_RE    = re.compile(r"SG_[A-Z]_(BUY|SELL)_", re.IGNORECASE)


def parse_lines(text: str):
    """Парсит строки — только TEST сделки. Возвращает (account, symbol, side, pct)."""
    results = []
    for line in text.splitlines():
        line = line.strip().replace('**', '').replace('__', '')
        if not line:
            continue
        if not TEST_RE.search(line):
            continue
        m = CLOSE_RE.search(line)
        if not m:
            continue
        acc_m   = ACCOUNT_RE.match(line)
        account = acc_m.group(1).strip() if acc_m else "unknown"
        sym_m   = SYMBOL_RE.search(line)
        if not sym_m:
            continue
        symbol  = sym_m.group(1).upper()
        side_m  = SIDE_RE.search(line)
        side    = side_m.group(1).upper() if side_m else "BUY"
        pct     = float(m.group(1))
        results.append((account, symbol, side, pct))
    return results


# trades[chat_id][account][symbol][side] = [(ts, pct), ...]
trades  = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list))))
alerted = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: datetime.min.replace(tzinfo=timezone.utc))))


async def send_message(http: aiohttp.ClientSession, text: str):
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ALERT_GROUP_ID, "text": text}
    try:
        async with http.post(url, json=payload) as resp:
            if resp.status != 200:
                log.error(f"Ошибка отправки: {resp.status} {await resp.text()}")
    except Exception as e:
        log.error(f"send_message error: {e}")


def calc_stats(records):
    """Считает суммарный % и winrate по списку записей."""
    total = sum(p for _, p in records)
    wins  = sum(1 for _, p in records if p > 0)
    wr    = round(wins / len(records) * 100) if records else 0
    return total, len(records), wr


def format_alert(account, symbol, group_title, long_rec, short_rec, total_pct, is_positive):
    sign   = "+" if total_pct >= 0 else ""
    header = "🟢 ДОБАВИТЬ НА РАБОЧИЙ ОРДЕР" if is_positive else "🔴 УБРАТЬ С ТЕСТОВОГО ОРДЕРА"

    long_total,  long_cnt,  long_wr  = calc_stats(long_rec)
    short_total, short_cnt, short_wr = calc_stats(short_rec)

    lines = [
        header, "",
        f"Аккаунт: {account}",
        f"Группа: {group_title}",
        f"Монета: #{symbol}",
        f"Суммарный % за день: {sign}{total_pct:.2f}%", "",
    ]

    if long_cnt > 0:
        ls = "+" if long_total >= 0 else ""
        lines.append(f"📈 Лонг: {ls}{long_total:.2f}% ({long_cnt} сд, WR {long_wr}%)")
        for ts, p in long_rec[-5:]:
            e = "✅" if p > 0 else "❌"
            s = "+" if p >= 0 else ""
            lines.append(f"   {e} {ts.strftime('%H:%M')} {s}{p:.2f}%")

    if short_cnt > 0:
        ss = "+" if short_total >= 0 else ""
        lines.append(f"📉 Шорт: {ss}{short_total:.2f}% ({short_cnt} сд, WR {short_wr}%)")
        for ts, p in short_rec[-5:]:
            e = "✅" if p > 0 else "❌"
            s = "+" if p >= 0 else ""
            lines.append(f"   {e} {ts.strftime('%H:%M')} {s}{p:.2f}%")

    return "\n".join(lines)


def process_trade(chat_id, account, symbol, side, pct, ts):
    """Добавляет сделку и возвращает суммарный % за окно по всем сторонам."""
    trades[chat_id][account][symbol][side].append((ts, pct))

    cutoff2 = ts - timedelta(minutes=WINDOW_MINUTES * 2)
    for s in ["BUY", "SELL"]:
        trades[chat_id][account][symbol][s] = [
            (t, p) for t, p in trades[chat_id][account][symbol][s] if t >= cutoff2
        ]

    cutoff = ts - timedelta(minutes=WINDOW_MINUTES)
    total  = sum(
        p for s in ["BUY", "SELL"]
        for t, p in trades[chat_id][account][symbol][s] if t >= cutoff
    )
    return total


def get_records(chat_id, account, symbol):
    """Возвращает записи лонг/шорт за окно."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)
    long_rec  = [(t, p) for t, p in trades[chat_id][account][symbol]["BUY"]  if t >= cutoff]
    short_rec = [(t, p) for t, p in trades[chat_id][account][symbol]["SELL"] if t >= cutoff]
    return long_rec, short_rec


async def load_history(client, group_ids):
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
                for account, symbol, side, pct in parsed:
                    process_trade(gid, account, symbol, side, pct, msg_time)
                    count += 1
            log.info(f"  📚 id={gid}: {count} TEST сделок")
            total_loaded += count
        except Exception as e:
            log.error(f"  ❌ Ошибка истории {gid}: {e}")
    log.info(f"✅ Итого из истории: {total_loaded} TEST сделок")


async def main():
    http   = aiohttp.ClientSession()
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()
    log.info("✅ Клиент запущен")

    group_ids = []
    async for dialog in client.iter_dialogs():
        if dialog.name in GROUPS_NAMES and dialog.id not in group_ids:
            group_ids.append(dialog.id)
            log.info(f"  📡 {dialog.name} (id={dialog.id})")

    if not group_ids:
        log.error("❌ Группы не найдены!")
        return

    log.info(f"✅ Подключено групп: {len(group_ids)}")
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

        for account, symbol, side, pct in parsed:
            total = process_trade(chat_id, account, symbol, side, pct, now)
            log.info(f"[{group_title}] {account} | {symbol} {side} {pct:+.2f}% | день: {total:+.2f}%")

            last = alerted[chat_id][account][symbol]
            if now - last < timedelta(minutes=COOLDOWN_MINUTES):
                continue

            if total >= ALERT_THRESHOLD:
                alerted[chat_id][account][symbol] = now
                long_rec, short_rec = get_records(chat_id, account, symbol)
                text_alert = format_alert(account, symbol, group_title,
                                          long_rec, short_rec, total, True)
                log.info(f"  → 🟢 ДОБАВИТЬ: {symbol} {total:+.2f}%")
                await send_message(http, text_alert)

            elif total <= ALERT_THRESHOLD_NEG:
                alerted[chat_id][account][symbol] = now
                long_rec, short_rec = get_records(chat_id, account, symbol)
                text_alert = format_alert(account, symbol, group_title,
                                          long_rec, short_rec, total, False)
                log.info(f"  → 🔴 УБРАТЬ: {symbol} {total:+.2f}%")
                await send_message(http, text_alert)

    log.info(f"🔍 Мониторинг TEST ордеров | +{ALERT_THRESHOLD}% / {ALERT_THRESHOLD_NEG}%")
    await client.run_until_disconnected()
    await http.close()


if __name__ == "__main__":
    asyncio.run(main())
