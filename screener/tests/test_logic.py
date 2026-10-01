import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from density import DensityTracker
from ticks import TickDetector


def book(mid, big_bid_at=None, big_usd=0):
    bids = [(mid - i, 1.0) for i in range(1, 50)]
    asks = [(mid + i, 1.0) for i in range(1, 50)]
    if big_bid_at:
        bids = [(p, big_usd / p if p == big_bid_at else q) for p, q in bids]
    return bids, asks


def test_density_detected_and_ranked():
    t = DensityTracker(min_usd=100_000, mult=5, max_dist_pct=10)
    b, a = book(1000, 990, 5_000_000)
    new = t.update("X", b, a, now=0)
    assert len(new) == 1 and new[0].price == 990
    r = t.ranked(now=300)
    assert r and r[0]["side"] == "bid" and r[0]["life_s"] == 300


def test_pulled_density_hurts_quality():
    t = DensityTracker(min_usd=100_000, mult=5, max_dist_pct=10)
    b, a = book(1000, 990, 5_000_000)
    t.update("X", b, a, now=0)
    b2, a2 = book(1000)               # убрали, цена не подходила
    t.update("X", b2, a2, now=10)
    assert t.stats["X"].pulled == 1 and t.stats["X"].quality == 0.0


def test_eaten_density():
    t = DensityTracker(min_usd=100_000, mult=5, max_dist_pct=10)
    b, a = book(1000, 990, 5_000_000)
    t.update("X", b, a, now=0)
    b2, a2 = book(985)                # цена ушла ниже уровня
    t.update("X", b2, a2, now=10)
    assert t.stats["X"].eaten == 1


def test_big_trade_and_burst():
    d = TickDetector(big_usd=100_000, burst_mult=3, burst_min_usd=200_000)
    for i in range(50):               # фон
        d.on_trade("X", 100, 1, False, ts=i * 1.0)
    ev = d.on_trade("X", 100, 3000, False, ts=60.0)   # $300k
    types = {e["type"] for e in ev}
    assert types == {"big", "burst"}
