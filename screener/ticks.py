"""Тиковые события: крупные принты и всплески объёма."""

import time
from collections import deque


class TickDetector:
    def __init__(self, big_usd=250_000, burst_mult=6.0, burst_min_usd=400_000, window_s=1.0):
        self.big_usd = big_usd
        self.burst_mult = burst_mult
        self.burst_min_usd = burst_min_usd
        self.window_s = window_s
        self.win: dict[str, deque] = {}       # (ts, usd, is_buy) за последнюю секунду
        self.base: dict[str, float] = {}      # EMA usd/сек
        self.last_burst: dict[str, float] = {}

    def on_trade(self, symbol: str, price: float, qty: float, is_buyer_maker: bool, ts: float | None = None):
        ts = time.time() if ts is None else ts
        usd = price * qty
        is_buy = not is_buyer_maker
        events = []

        if usd >= self.big_usd:
            events.append({"type": "big", "symbol": symbol, "side": "buy" if is_buy else "sell",
                           "usd": round(usd), "price": price, "ts": ts})

        w = self.win.setdefault(symbol, deque())
        w.append((ts, usd, is_buy))
        while w and ts - w[0][0] > self.window_s:
            w.popleft()
        vol = sum(u for _, u, _ in w)
        base = self.base.get(symbol, vol)
        self.base[symbol] = base * 0.995 + vol * 0.005

        if (vol >= self.burst_min_usd and vol >= base * self.burst_mult
                and ts - self.last_burst.get(symbol, 0) > 5):
            buy = sum(u for _, u, b in w if b)
            self.last_burst[symbol] = ts
            events.append({"type": "burst", "symbol": symbol, "usd": round(vol),
                           "buy_pct": round(buy / vol * 100), "price": price, "ts": ts})
        return events
