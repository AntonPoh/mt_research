"""Детектор алгоритмических серий сделок (TWAP / периодические / одинаковые принты).

Серия = (venue, symbol, actor, side). actor - адрес кошелька (Hyperliquid) либо
"q<qty>" (Binance, где адресов нет: ловим повторяющиеся принты одинакового размера).
Подряд идущие сделки ближе gap_s склеиваются в один "слайс".
"""

import time
from dataclasses import dataclass, field
from statistics import median


@dataclass
class Series:
    venue: str
    symbol: str
    actor: str
    side: str                      # "buy" | "sell"
    slices: list = field(default_factory=list)   # [start_ts, usd, price_vwap_num, last_ts]
    cur: list | None = None        # текущий незакрытый слайс (та же структура)
    first_price: float = 0.0
    last_price: float = 0.0
    last_ts: float = 0.0
    hits: int = 0


class AlgoTracker:
    def __init__(self, gap_s=3.0, min_slices=4, min_total_usd=20_000,
                 twap_range=(24.0, 40.0), ttl_s=1800, max_slices=300, tol=0.2):
        self.gap_s = gap_s
        self.min_slices = min_slices
        self.min_total_usd = min_total_usd
        self.twap_range = twap_range
        self.ttl_s = ttl_s
        self.max_slices = max_slices
        self.tol = tol
        self.series: dict[tuple, Series] = {}

    def feed(self, venue, symbol, actor, side, ts, usd, price):
        key = (venue, symbol, actor, side)
        s = self.series.get(key)
        if s is None:
            s = self.series[key] = Series(venue, symbol, actor, side, first_price=price)
        s.last_price, s.last_ts = price, ts
        c = s.cur
        if c is not None and ts - c[3] <= self.gap_s:
            c[1] += usd
            c[2] += price * usd
            c[3] = ts
        else:
            if c is not None:
                s.slices.append(c)
                if len(s.slices) > self.max_slices:
                    del s.slices[: len(s.slices) - self.max_slices]
            s.cur = [ts, usd, price * usd, ts]
        return s

    def _evaluate(self, s: Series, now: float):
        sl = s.slices + ([s.cur] if s.cur else [])
        if len(sl) < self.min_slices:
            return None
        total = sum(x[1] for x in sl)
        if total < self.min_total_usd:
            return None
        starts = [x[0] for x in sl]
        iv = [b - a for a, b in zip(starts, starts[1:]) if b - a > 0]
        if len(iv) < self.min_slices - 1:
            return None
        m = median(iv)
        if m <= 0:
            return None
        regular = sum(1 for d in iv if abs(d - m) <= self.tol * m) / len(iv)
        sizes = [x[1] for x in sl]
        msz = median(sizes)
        size_reg = sum(1 for z in sizes if abs(z - msz) <= 0.3 * msz) / len(sizes)
        if regular < 0.6:
            return None
        duration = max(starts[-1] - starts[0], 1.0)
        if s.venue == "hl" and self.twap_range[0] <= m <= self.twap_range[1] and size_reg >= 0.5:
            kind = "twap"
        else:
            kind = "periodic"
        sign = 1 if s.side == "buy" else -1
        impact_bps = (s.last_price - s.first_price) / s.first_price * 1e4 * sign if s.first_price else 0.0
        active = now - s.last_ts <= max(2.5 * m, 10)
        score = regular * (0.5 + size_reg) * min(total / 100_000, 20) * (1.0 if active else 0.4)
        return {
            "venue": s.venue, "symbol": s.symbol, "actor": s.actor, "side": s.side, "kind": kind,
            "slices": len(sl), "interval_s": round(m, 1), "regularity": round(regular, 2),
            "size_reg": round(size_reg, 2), "total_usd": round(total),
            "slice_usd": round(msz), "rate_usd_min": round(total / duration * 60),
            "impact_bps": round(impact_bps, 1), "active": active,
            "age_s": int(now - starts[0]), "last_s": int(now - s.last_ts),
            "price": s.last_price, "score": round(score, 2),
        }

    def snapshot(self, now: float | None = None, limit=100, only_active=False):
        now = time.time() if now is None else now
        for k in [k for k, s in self.series.items() if now - s.last_ts > self.ttl_s]:
            del self.series[k]
        out = []
        for s in self.series.values():
            r = self._evaluate(s, now)
            if r and (r["active"] or not only_active):
                out.append(r)
        out.sort(key=lambda r: r["score"], reverse=True)
        return out[:limit]
