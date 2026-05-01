"""
Telegram Trade Monitor — Bot API версия с debug логированием
"""

import os
import asyncio
import re
import json
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from aiohttp import web
import aiohttp

BOT_TOKEN          = os.environ.get("BOT_TOKEN", "")
ALERT_GROUP_ID     = int(os.environ.get("ALERT_GROUP_ID", "0"))
ALERT_THRESHOLD    = float(os.environ.get("ALERT_THRESHOLD", "1.5"))
WINDOW_MINUTES     = int(os.environ.get("WINDOW_MINUTES", "30"))
COOLDOWN_MINUTES   = int(os.environ.get("COOLDOWN_MINUTES", "30"))
PORT               = int(os.environ.get("PORT", "8080"))
DEBUG              = os.environ.get("DEBUG", "true").lower() == "true"

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


async def process_update(update: dict, session: aiohttp.ClientSession):
    # DEBUG — логируем всю структуру update
    if DEBUG:
        log.info(f"RAW UPDATE: {json.dumps(update, ensure_ascii=False)[:500]}")

    # Пробуем все возможные типы сообщений
    msg = (update.get("message") or
           update.get("channel_post") or
           update.get("edited_message") or
           update.get("edited_channel_post"))

    if not msg:
        log.info(f"UPDATE KEYS: {list(update.keys())}")
        return

    # Пробуем text и caption
    text = msg.get("text") or msg.get("caption") or ""

    chat        = msg.get("chat", {})
    chat_id     = chat.get("id")
    group_title = chat.get("title", str(chat_id))

    log.info(f"MSG from [{group_title}] text_len={len(text)} chat_id={chat_id}")

    if not text or not chat_id:
        log.info(f"MSG KEYS: {list(msg.keys())}")
        return

    parsed = parse_lines(text)
    log.info(f"PARSED: {parsed}")

    if not parsed:
        # Логируем первые 200 символов текста для отладки
        log.info(f"NO PARSE: {repr(text[:200])}")
        return

    now = datetime.utcnow()

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
        await send_alert(session, text_alert)


async def webhook_handler(request: web.Request) -> web.Response:
    session = request.app["session"]
    try:
        update = await request.json()
        asyncio.create_task(process_update(update, session))
    except Exception as e:
        log.error(f"webhook_handler error: {e}")
    return web.Response(text="ok")


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="✅ Trade Monitor is running")


async def on_startup(app: web.Application):
    session = aiohttp.ClientSession()
    app["session"] = session

    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if railway_url:
        webhook_url = f"https://{railway_url}/webhook"
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
        async with session.post(url, json={"url": webhook_url}) as resp:
            data = await resp.json()
            if data.get("ok"):
                log.info(f"✅ Webhook установлен: {webhook_url}")
            else:
                log.error(f"❌ Ошибка webhook: {data}")

    log.info(f"🔍 Монитор запущен | порог {ALERT_THRESHOLD}% за {WINDOW_MINUTES} мин")


async def on_cleanup(app: web.Application):
    await app["session"].close()


def main():
    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)
    app.router.add_get("/", health_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
