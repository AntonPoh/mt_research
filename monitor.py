"""
Telegram Trade Monitor — Bot API версия
========================================
Бот добавлен в 12 торговых групп как администратор.
Читает сообщения, считает суммарный % по монетам за 30 минут,
шлёт алерт в отдельную группу когда монета набирает >= 1.5%.

Деплой: Railway
"""

import os
import asyncio
import re
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from aiohttp import web
import aiohttp

# ─────────────────────────────────────────
#  НАСТРОЙКИ — через переменные окружения Railway
# ─────────────────────────────────────────

BOT_TOKEN          = os.environ.get("BOT_TOKEN", "")
ALERT_GROUP_ID     = int(os.environ.get("ALERT_GROUP_ID", "0"))
ALERT_THRESHOLD    = float(os.environ.get("ALERT_THRESHOLD", "1.5"))
WINDOW_MINUTES     = int(os.environ.get("WINDOW_MINUTES", "30"))
COOLDOWN_MINUTES   = int(os.environ.get("COOLDOWN_MINUTES", "30"))
PORT               = int(os.environ.get("PORT", "8080"))

# ─────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
#  ПАРСИНГ СООБЩЕНИЙ
# ─────────────────────────────────────────

# Profit: ⬆️ Profit +0.05$ (+12.8%) (U) #BSBUSDT sold 31 x 0.315 (+0.5%)
# Loss:   ⬇️ Loss -0.03$ (-7.7%) (U) #BSBUSDT sold 30 x 0.325 (-0.3%)

CLOSE_RE = re.compile(
    r"(?:Profit|Loss)"
    r".+?"
    r"#(\w+)"           # символ монеты
    r".+?"
    r"\(([+-][\d.]+)%\)\s*$",   # % последний в строке
    re.IGNORECASE | re.DOTALL,
)

ACCOUNT_RE = re.compile(r"^([^,]+),")


def parse_close(text: str):
    """Возвращает (account, symbol, pct) или None."""
    clean = text.strip()
    acc_m = ACCOUNT_RE.match(clean)
    account = acc_m.group(1).strip() if acc_m else "unknown"
    m = CLOSE_RE.search(clean)
    if not m:
        return None
    symbol = m.group(1).upper()
    pct    = float(m.group(2))
    return account, symbol, pct


# ─────────────────────────────────────────
#  СОСТОЯНИЕ
# ─────────────────────────────────────────

# trades[chat_id][account][symbol] = [(datetime, pct), ...]
trades  = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

# alerted[chat_id][account][symbol] = datetime последнего алерта
alerted = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: datetime.min)))


# ─────────────────────────────────────────
#  ОТПРАВКА АЛЕРТОВ
# ─────────────────────────────────────────

async def send_alert(session: aiohttp.ClientSession, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": ALERT_GROUP_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error(f"Ошибка отправки: {resp.status} {body}")
    except Exception as e:
        log.error(f"send_alert error: {e}")


def format_alert(account, symbol, total_pct, group_title, records):
    sign = "+" if total_pct >= 0 else ""
    now  = datetime.utcnow()
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)
    recent = [(ts, p) for ts, p in records if ts >= cutoff]

    lines = [
        f"🚨 *Алерт по монете*",
        f"",
        f"📊 *Аккаунт:* `{account}`",
        f"📣 *Группа:* {group_title}",
        f"💎 *Монета:* #{symbol}",
        f"📈 *Суммарный % за {WINDOW_MINUTES} мин:* `{sign}{total_pct:.2f}%`",
        f"",
        f"*Сделки за окно:*",
    ]
    for ts, p in recent[-10:]:
        s = "+" if p >= 0 else ""
        lines.append(f"  `{ts.strftime('%H:%M')}` → `{s}{p:.1f}%`")
    return "\n".join(lines)


# ─────────────────────────────────────────
#  ОБРАБОТКА ВХОДЯЩИХ UPDATES
# ─────────────────────────────────────────

async def process_update(update: dict, session: aiohttp.ClientSession):
    # Берём сообщение (обычное или из канала/группы)
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return

    text     = msg.get("text", "")
    chat     = msg.get("chat", {})
    chat_id  = chat.get("id")
    group_title = chat.get("title", str(chat_id))

    if not text or not chat_id:
        return

    result = parse_close(text)
    if result is None:
        return

    account, symbol, pct = result
    now = datetime.utcnow()

    # Записываем сделку
    trades[chat_id][account][symbol].append((now, pct))

    # Чистим старые (старше 2 окон)
    cutoff2 = now - timedelta(minutes=WINDOW_MINUTES * 2)
    trades[chat_id][account][symbol] = [
        (ts, p) for ts, p in trades[chat_id][account][symbol] if ts >= cutoff2
    ]

    # Суммарный % за окно
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)
    total  = sum(p for ts, p in trades[chat_id][account][symbol] if ts >= cutoff)

    log.info(f"[{group_title}] {account} | {symbol} {pct:+.1f}% | окно: {total:+.2f}%")

    # Проверяем порог
    if total < ALERT_THRESHOLD:
        return

    # Проверяем кулдаун
    last = alerted[chat_id][account][symbol]
    if now - last < timedelta(minutes=COOLDOWN_MINUTES):
        log.info(f"  → кулдаун {symbol}, пропускаем")
        return

    # Шлём алерт
    alerted[chat_id][account][symbol] = now
    text_alert = format_alert(account, symbol, total, group_title,
                               trades[chat_id][account][symbol])
    log.info(f"  → 🚨 АЛЕРТ: {symbol} {total:+.2f}% [{account}] [{group_title}]")
    await send_alert(session, text_alert)


# ─────────────────────────────────────────
#  WEBHOOK СЕРВЕР
# ─────────────────────────────────────────

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

    # Регистрируем webhook
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
    else:
        log.warning("RAILWAY_PUBLIC_DOMAIN не задан — webhook не установлен")

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
