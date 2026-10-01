"""Hyperliquid: топ-монеты по обороту + поток сделок с адресами (для TWAP/алго-сканера)."""

import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("screener.hl")
INFO = "https://api.hyperliquid.xyz/info"
WS = "wss://api.hyperliquid.xyz/ws"


async def top_coins(http: aiohttp.ClientSession, n: int) -> list[str]:
    async with http.post(INFO, json={"type": "metaAndAssetCtxs"}) as r:
        meta, ctxs = await r.json()
    rows = [(float(c.get("dayNtlVlm") or 0), u["name"])
            for u, c in zip(meta["universe"], ctxs) if not u.get("isDelisted")]
    rows.sort(reverse=True)
    return [name for _, name in rows[:n]]


def parse_trade(t: dict):
    """-> (coin, taker_addr, side, usd, price, ts) | None.

    users = [buyer, seller]; side - сторона агрессора (B = покупатель забирает).
    """
    users = t.get("users")
    if not users or len(users) != 2:
        return None
    buy = t["side"] == "B"
    price, size = float(t["px"]), float(t["sz"])
    return (t["coin"], users[0] if buy else users[1], "buy" if buy else "sell",
            price * size, price, t["time"] / 1000)


async def trades_loop(http: aiohttp.ClientSession, coins: list[str], on_trade):
    while True:
        try:
            async with http.ws_connect(WS, heartbeat=20) as ws:
                for c in coins:
                    await ws.send_json({"method": "subscribe",
                                        "subscription": {"type": "trades", "coin": c}})
                log.info("hyperliquid connected (%d coins)", len(coins))
                async for m in ws:
                    if m.type != aiohttp.WSMsgType.TEXT:
                        continue
                    d = json.loads(m.data)
                    if d.get("channel") != "trades":
                        continue
                    for t in d["data"]:
                        p = parse_trade(t)
                        if p:
                            await on_trade(*p)
        except Exception as e:
            log.error("hyperliquid ws error: %s; reconnect in 3s", e)
            await asyncio.sleep(3)
