"""Детектор плотностей в стакане + ранжирование по поведению цены."""

import time
from dataclasses import dataclass, field
from statistics import median


@dataclass
class Level:
    symbol: str
    side: str            # "bid" | "ask"
    price: float
    usd: float
    first_seen: float
    last_seen: float
    max_usd: float
    start_mid: float
    best_dist_pct: float  # минимальная дистанция цены до уровня за время жизни


@dataclass
class SymbolStats:
    touched: int = 0     # уровень сняли после того как цена дошла до него (настоящая плотность)
    pulled: int = 0      # уровень сняли, цена не дошла (спуфинг / "всратая")
    eaten: int = 0       # цена прошла сквозь уровень

    @property
    def total(self):
        return self.touched + self.pulled + self.eaten

    @property
    def quality(self) -> float:
        """0..1: доля плотностей, которые реально держали/съедались, а не убегали."""
        return 0.5 if self.total == 0 else (self.touched + self.eaten) / self.total


class DensityTracker:
    def __init__(self, min_usd=300_000, mult=8.0, max_dist_pct=3.0, touch_pct=0.08):
        self.min_usd = min_usd
        self.mult = mult
        self.max_dist_pct = max_dist_pct
        self.touch_pct = touch_pct
        self.active: dict[tuple, Level] = {}
        self.stats: dict[str, SymbolStats] = {}

    def update(self, symbol: str, bids, asks, now: float | None = None, min_usd: float | None = None):
        """bids/asks: списки (price, qty). Возвращает список новых плотностей."""
        now = time.time() if now is None else now
        if not bids or not asks:
            return []
        mid = (bids[0][0] + asks[0][0]) / 2
        new, seen = [], set()

        for side, book in (("bid", bids), ("ask", asks)):
            usd = [(p, p * q) for p, q in book]
            med = median(u for _, u in usd) or 1.0
            thr = max(min_usd if min_usd is not None else self.min_usd, med * self.mult)
            for p, u in usd:
                dist = abs(p - mid) / mid * 100
                if dist > self.max_dist_pct or u < thr:
                    continue
                key = (symbol, side, p)
                seen.add(key)
                lv = self.active.get(key)
                if lv is None:
                    lv = Level(symbol, side, p, u, now, now, u, mid, dist)
                    self.active[key] = lv
                    new.append(lv)
                else:
                    lv.last_seen, lv.usd = now, u
                    lv.max_usd = max(lv.max_usd, u)
                    lv.best_dist_pct = min(lv.best_dist_pct, dist)

        # проверяем исчезнувшие
        st = self.stats.setdefault(symbol, SymbolStats())
        for key in [k for k in self.active if k[0] == symbol and k not in seen]:
            lv = self.active.pop(key)
            crossed = (mid <= lv.price) if lv.side == "bid" else (mid >= lv.price)
            if crossed:
                st.eaten += 1
            elif lv.best_dist_pct <= self.touch_pct:
                st.touched += 1
            else:
                st.pulled += 1
        return new

    def ranked(self, now: float | None = None, limit=50):
        now = time.time() if now is None else now
        out = []
        for lv in self.active.values():
            life = now - lv.first_seen
            q = self.stats.get(lv.symbol, SymbolStats()).quality
            # крупнее, старше, ближе к цене и от символа, где плотности не убегают -> выше
            score = (lv.usd / 1e6) * (1 + min(life, 900) / 300) * (0.5 + q)
            out.append({
                "symbol": lv.symbol, "side": lv.side, "price": lv.price,
                "usd": round(lv.usd), "life_s": int(life),
                "dist_pct": round(lv.best_dist_pct, 3),
                "quality": round(q, 2), "score": round(score, 2),
            })
        out.sort(key=lambda r: r["score"], reverse=True)
        return out[:limit]
