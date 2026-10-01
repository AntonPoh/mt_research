import os, sys, random
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from algos import AlgoTracker


def test_twap_detected_with_impact():
    t = AlgoTracker()
    for i in range(12):
        t.feed("hl", "BTC", "0xabc", "buy", 1000 + i * 30 + random.uniform(-1, 1), 5000, 100 + i * 0.1)
    r = t.snapshot(now=1000 + 11 * 30 + 5)
    assert len(r) == 1 and r[0]["kind"] == "twap" and r[0]["active"]
    assert r[0]["impact_bps"] > 0 and r[0]["slices"] == 12


def test_random_flow_not_detected():
    random.seed(1)
    t = AlgoTracker()
    ts = 0
    for _ in range(40):
        ts += random.uniform(1, 90)
        t.feed("hl", "BTC", "0xrnd", "sell", ts, random.uniform(1000, 9000), 100)
    assert t.snapshot(now=ts) == []


def test_slices_merge_within_gap():
    t = AlgoTracker(gap_s=3)
    for i in range(6):
        base = i * 30
        t.feed("hl", "ETH", "0x1", "buy", base, 3000, 10)
        t.feed("hl", "ETH", "0x1", "buy", base + 1, 3000, 10)   # тот же слайс
    r = t.snapshot(now=6 * 30)
    assert r and r[0]["slices"] == 6 and r[0]["slice_usd"] == 6000


def test_finished_series_inactive():
    t = AlgoTracker()
    for i in range(8):
        t.feed("bn", "SOLUSDT", "q10", "buy", i * 20, 8000, 50)
    r = t.snapshot(now=7 * 20 + 600)
    assert r and not r[0]["active"] and r[0]["kind"] == "periodic"
    assert t.snapshot(now=7 * 20 + 600, only_active=True) == []


def test_hl_parse_trade():
    from hyperliquid import parse_trade
    t = {"coin": "BTC", "side": "B", "px": "100", "sz": "2", "time": 1700000000000, "users": ["0xbuy", "0xsell"]}
    assert parse_trade(t) == ("BTC", "0xbuy", "buy", 200.0, 100.0, 1700000000.0)
    t["side"] = "A"
    assert parse_trade(t)[1:3] == ("0xsell", "sell")
    assert parse_trade({**t, "users": None}) is None
