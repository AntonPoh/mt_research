"""Scalp screener: Binance Futures ticks + density -> web dashboard + Telegram."""

import asyncio
import json
import logging
import os
import sys
import time
from collections import deque

import aiohttp
from aiohttp import web

from algos import AlgoTracker
import hyperliquid
from density import DensityTracker
from ticks import TickDetector

PORT            = int(os.environ.get("PORT", "8080"))
SYMBOLS_N       = int(os.environ.get("SYMBOLS_N", "20"))
DEPTH_EVERY_S   = float(os.environ.get("DEPTH_EVERY_S", "10"))
DEPTH_LIMIT     = int(os.environ.get("DEPTH_LIMIT", "500"))
BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
ALERT_CHAT_ID   = os.environ.get("ALERT_CHAT_ID", "")
ALERT_MIN_SCORE = float(os.environ.get("ALERT_MIN_SCORE", "3.0"))
AUTH_TOKEN      = os.environ.get("AUTH_TOKEN", "")   # если задан: /?token=...
REST            = os.environ.get("BINANCE_REST", "https://fapi.binance.com")
WSS             = os.environ.get("BINANCE_WS", "wss://fstream.binance.com")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("screener")

density = DensityTracker(
    min_usd=float(os.environ.get("DENSITY_MIN_USD", "300000")),
    mult=float(os.environ.get("DENSITY_MULT", "8")),
    max_dist_pct=float(os.environ.get("DENSITY_MAX_DIST_PCT", "2")),
)
ticks = TickDetector(
    big_usd=float(os.environ.get("TICK_BIG_USD", "250000")),
    burst_min_usd=float(os.environ.get("TICK_BURST_USD", "400000")),
)
HL_COINS_N      = int(os.environ.get("HL_COINS_N", "40"))
HL_ALERT_USD    = float(os.environ.get("HL_TWAP_ALERT_USD", "100000"))
ALGO_MIN_PRINT  = float(os.environ.get("ALGO_MIN_PRINT_USD", "5000"))
algos = {
    "hl": AlgoTracker(gap_s=3.0, min_slices=4, min_total_usd=20_000, ttl_s=1800),
    "bn": AlgoTracker(gap_s=0.5, min_slices=8, min_total_usd=100_000, ttl_s=900),
}
algo_alerted: dict[tuple, float] = {}
events: deque = deque(maxlen=300)
clients: set[web.WebSocketResponse] = set()
alerted: dict[tuple, float] = {}
vol24: dict[str, float] = {}
DENSITY_VOL_FRAC = float(os.environ.get("DENSITY_VOL_FRAC", "0.001"))   # доля суточного оборота
DENSITY_FLOOR    = float(os.environ.get("DENSITY_FLOOR_USD", "150000"))


async def broadcast(msg: dict):
    data = json.dumps(msg)
    for ws in list(clients):
        try:
            await ws.send_str(data)
        except Exception:
            clients.discard(ws)


async def telegram(http: aiohttp.ClientSession, text: str):
    if not (BOT_TOKEN and ALERT_CHAT_ID):
        return
    try:
        async with http.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                             json={"chat_id": ALERT_CHAT_ID, "text": text}) as r:
            if r.status != 200:
                log.error("telegram %s %s", r.status, await r.text())
    except Exception as e:
        log.error("telegram error: %s", e)


async def top_symbols(http) -> list[str]:
    async with http.get(f"{REST}/fapi/v1/ticker/24hr") as r:
        data = await r.json()
    usdt = [d for d in data if d["symbol"].endswith("USDT") and d["symbol"].isascii()]
    usdt.sort(key=lambda d: float(d["quoteVolume"]), reverse=True)
    top = usdt[:SYMBOLS_N]
    for d in top:
        vol24[d["symbol"]] = float(d["quoteVolume"])
    return [d["symbol"] for d in top]


async def ticks_loop(http, symbols):
    streams = "/".join(f"{s.lower()}@aggTrade" for s in symbols)
    url = f"{WSS}/stream?streams={streams}"
    while True:
        try:
            async with http.ws_connect(url, heartbeat=20) as ws:
                log.info("ticks connected (%d symbols)", len(symbols))
                async for m in ws:
                    if m.type != aiohttp.WSMsgType.TEXT:
                        continue
                    d = json.loads(m.data)["data"]
                    usd = float(d["p"]) * float(d["q"])
                    if usd >= ALGO_MIN_PRINT:
                        algos["bn"].feed("bn", d["s"], f"q{d['q']}", "sell" if d["m"] else "buy",
                                         d["T"] / 1000, usd, float(d["p"]))
                    for ev in ticks.on_trade(d["s"], float(d["p"]), float(d["q"]), d["m"], d["T"] / 1000):
                        events.append(ev)
                        await broadcast({"kind": "tick", "event": ev})
                        if ev["type"] == "burst":
                            await telegram(http, f"⚡ {ev['symbol']} всплеск ${ev['usd']:,} "
                                                 f"(buy {ev['buy_pct']}%) @ {ev['price']}")
        except Exception as e:
            log.error("ticks ws error: %s; reconnect in 3s", e)
            await asyncio.sleep(3)


async def depth_loop(http, symbols):
    while True:
        started = time.time()
        for sym in symbols:
            try:
                async with http.get(f"{REST}/fapi/v1/depth",
                                    params={"symbol": sym, "limit": DEPTH_LIMIT}) as r:
                    if r.status != 200:
                        log.warning("depth %s -> %s", sym, r.status)
                        continue
                    d = await r.json()
                bids = [(float(p), float(q)) for p, q in d["bids"]]
                asks = [(float(p), float(q)) for p, q in d["asks"]]
                min_usd = max(DENSITY_FLOOR, vol24.get(sym, 0) * DENSITY_VOL_FRAC)
                for lv in density.update(sym, bids, asks, min_usd=min_usd):
                    key = (lv.symbol, lv.side, lv.price)
                    row = next((x for x in density.ranked() if (x["symbol"], x["side"], x["price"]) == key), None)
                    if row and row["score"] >= ALERT_MIN_SCORE and time.time() - alerted.get(key, 0) > 1800:
                        alerted[key] = time.time()
                        await telegram(http, f"🧱 {lv.symbol} {lv.side.upper()} ${lv.usd:,.0f} "
                                             f"@ {lv.price} (score {row['score']}, q {row['quality']})")
            except Exception as e:
                log.error("depth %s: %s", sym, e)
        await broadcast({"kind": "density", "rows": density.ranked()})
        log.info("depth cycle %.1fs, active=%d", time.time() - started, len(density.active))
        await asyncio.sleep(max(0.0, DEPTH_EVERY_S - (time.time() - started)))


async def hl_on_trade(coin, actor, side, usd, price, ts):
    algos["hl"].feed("hl", coin, actor, side, ts, usd, price)


async def algo_loop(http):
    while True:
        await asyncio.sleep(5)
        rows = [r for t in algos.values() for r in t.snapshot()]
        rows.sort(key=lambda r: r["score"], reverse=True)
        app_state["algos"] = rows[:150]
        app_state["n"] = app_state.get("n", 0) + 1
        if app_state["n"] % 12 == 0:
            log.info("algos: total=%d active=%d twap=%d | hl series=%d bn series=%d", len(rows),
                     sum(r["active"] for r in rows), sum(r["kind"] == "twap" for r in rows),
                     len(algos["hl"].series), len(algos["bn"].series))
        await broadcast({"kind": "algos", "rows": app_state["algos"]})
        for r in rows:
            key = (r["venue"], r["symbol"], r["actor"], r["side"])
            if (r["kind"] == "twap" and r["active"] and r["total_usd"] >= HL_ALERT_USD
                    and time.time() - algo_alerted.get(key, 0) > 3600):
                algo_alerted[key] = time.time()
                await telegram(http, f"🤖 TWAP {r['symbol']} {r['side'].upper()} {r['actor'][:8]}… "
                                     f"${r['total_usd']:,} ({r['slices']} слайсов по ${r['slice_usd']:,}, "
                                     f"impact {r['impact_bps']} bps)")


app_state: dict = {"algos": []}


def authorized(req: web.Request) -> bool:
    return not AUTH_TOKEN or req.query.get("token") == AUTH_TOKEN


async def index(req):
    if not authorized(req):
        return web.Response(status=401, text="unauthorized")
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


async def state(req):
    if not authorized(req):
        return web.Response(status=401, text="unauthorized")
    return web.json_response({"density": density.ranked(), "ticks": list(events)[-100:],
                              "algos": app_state["algos"]})


async def ws_handler(req):
    if not authorized(req):
        return web.Response(status=401, text="unauthorized")
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    clients.add(ws)
    await ws.send_str(json.dumps({"kind": "density", "rows": density.ranked()}))
    await ws.send_str(json.dumps({"kind": "algos", "rows": app_state["algos"]}))
    try:
        async for _ in ws:
            pass
    finally:
        clients.discard(ws)
    return ws


async def start_hl(http):
    try:
        coins = await hyperliquid.top_coins(http, HL_COINS_N)
        log.info("hl coins: %s", ",".join(coins))
        await hyperliquid.trades_loop(http, coins, hl_on_trade)
    except Exception as e:
        log.error("hyperliquid start failed: %s", e)


async def on_start(app):
    http = aiohttp.ClientSession()
    app["http"] = http
    symbols = await top_symbols(http)
    log.info("symbols: %s", ",".join(symbols))
    app["tasks"] = [asyncio.create_task(ticks_loop(http, symbols)),
                    asyncio.create_task(depth_loop(http, symbols)),
                    asyncio.create_task(algo_loop(http)),
                    asyncio.create_task(start_hl(http))]


async def on_stop(app):
    for t in app["tasks"]:
        t.cancel()
    await app["http"].close()


def make_app():
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/api/state", state), web.get("/ws", ws_handler)])
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=PORT)
