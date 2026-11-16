"""The three rules on hand-built prices. Run from buylow/: python -m pytest -q"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import models  # noqa: E402
from models import Grid, dip_decide, dip_indicators, run_dip, run_grid, run_stretch, stretch_indicators  # noqa: E402


# --- Dip ---------------------------------------------------------------------

def daily(closes, start="2020-01-01"):
    dates = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({"date": dates, "open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1e6})


def uptrend_then_dip():
    closes = list(np.linspace(80, 100, 230))  # well above its 200-day average
    closes += [97, 94]  # two sharp down days: RSI(2) near 0
    closes += [95, 99, 101, 102]  # the bounce
    return closes


def test_rsi_of_two_down_days_is_oversold():
    df = dip_indicators(daily(uptrend_then_dip()))
    i = 231  # the second down day
    assert df.loc[i, "rsi"] < config.DIP_RSI_BELOW and df.loc[i, "buy"]
    assert not df.loc[229, "buy"]  # a steady climb is not a dip


def test_dip_buys_next_open_and_sells_after_the_bounce():
    df = dip_indicators(daily(uptrend_then_dip()))
    trades, equity = run_dip({"X": df}, df["date"].iloc[0], df["date"].iloc[-1], cash=1500, cost=0)
    assert len(trades) == 1
    t = trades.iloc[0]
    # One 3% drop after a slow climb is already oversold: it buys the next open (94)
    assert t["entry_date"] == df["date"][231] and t["entry"] == 94
    # 99 closes above the 5-day average (about 97), so it sells at the following open: 101
    assert t["exit_date"] == df["date"][234] and t["exit"] == 101
    assert t["shares"] == pytest.approx(500 / 94)  # a third of the sleeve
    assert equity["equity"].iloc[-1] == pytest.approx(1500 + 500 / 94 * 7)


def test_dip_decide_sells_on_bounce_or_after_max_days_and_ranks_buys():
    row = lambda buy=False, rsi=50, bounced=False: {"buy": buy, "rsi": rsi, "bounced": bounced}
    rows = {"A": row(bounced=True), "B": row(), "C": row(), "D": row(True, 5), "E": row(True, 2), "F": row(True, 8)}
    sells, buys = dip_decide(rows, {"A": 3, "B": config.DIP_MAX_DAYS, "C": 2})
    assert sells == ["A", "B"]
    assert buys == ["E", "D"]  # two slots free, most oversold first


def test_dip_never_buys_more_than_its_slots():
    rows = {s: {"buy": True, "rsi": i, "bounced": False} for i, s in enumerate("ABCDE")}
    _, buys = dip_decide(rows, {})
    assert buys == ["A", "B", "C"]


# --- Grid --------------------------------------------------------------------

def bar(t, o, h, l, c):
    return {"timestamp": pd.Timestamp(f"2024-01-02 {t}", tz="America/New_York"), "open": o, "high": h, "low": l,
            "close": c}


def test_grid_buys_a_slice_and_sells_it_one_percent_up():
    g = Grid(anchor=100.0, cash=1000, slice_dollars=200)
    assert [p for side, _, p in g.orders()] == [99.0, 98.0, 97.0, 96.0, 95.0]
    g.on_bar(bar("10:00", 99.5, 99.6, 98.98, 99.2))  # trades through 99 by 2 cents
    s1 = g.slices[0]
    assert s1.held and s1.buy_price == 99.0 and s1.qty == pytest.approx(200 / 99)
    assert not g.slices[1].held  # 98 not reached
    g.on_bar(bar("10:01", 99.2, 99.99, 99.1, 99.9))  # 99.99 touched but not traded through
    assert s1.held
    g.on_bar(bar("10:02", 99.9, 100.0, 99.8, 100.0))
    assert not s1.held and g.trades[0]["exit"] == 99.99
    assert g.cash == pytest.approx(1000 + 200 / 99 * 0.99)


def test_grid_fills_at_a_lower_open_and_never_sells_in_the_buy_bar():
    g = Grid(anchor=100.0, cash=1000, slice_dollars=200)
    g.on_bar(bar("09:30", 97.5, 101.0, 97.4, 100.5))  # gaps through 99 and 98, then runs past both targets
    assert g.slices[0].buy_price == 97.5 and g.slices[1].buy_price == 97.5
    assert g.slices[0].held and not g.trades


def test_grid_moves_up_only_when_empty_and_past_the_threshold():
    g = Grid(anchor=100.0, cash=1000, slice_dollars=200)
    g.on_bar(bar("10:00", 100, 100.2, 100, 100.1))
    assert g.anchor == 100.0  # 0.2% is under the 0.25% threshold
    g.on_bar(bar("10:01", 100.1, 100.3, 100.1, 100.3))
    assert g.anchor == 100.3
    g.on_bar(bar("10:02", 100.3, 100.3, 99.2, 99.3))  # buys slice 1 at 99.30
    g.on_bar(bar("10:03", 99.3, 101.5, 99.3, 99.5))  # sells it at 100.29, but...
    assert g.trades and g.anchor == 101.5  # ...it's empty again, so the ladder follows the price up


def test_grid_stops_buying_when_the_cash_is_used():
    g = Grid(anchor=100.0, cash=500, slice_dollars=200)
    g.on_bar(bar("10:00", 100, 100, 90, 90))
    assert sum(s.held for s in g.slices) == 2


def test_run_grid_days():
    rows = []
    for d, path in (("2024-01-02", [100, 98.9, 99.5]), ("2024-01-03", [99.6, 100.1, 100.2])):
        for i, p in enumerate(path):
            ts = pd.Timestamp(f"{d} 09:3{i}", tz="America/New_York")
            rows.append({"timestamp": ts, "date": pd.Timestamp(d), "open": p, "high": p, "low": p, "close": p})
    trades, equity, held = run_grid(pd.DataFrame(rows), "2024-01-01", "2024-01-31")
    assert len(trades) == 1 and not held
    # sell level 99.89; the second day's 100.10 bar opens above it, so it fills at that open
    assert trades.iloc[0]["entry"] == 98.9 and trades.iloc[0]["exit"] == 100.1


# --- Stretch -----------------------------------------------------------------

def day_bars(prices, symbol_vol=1000, date="2024-01-02"):
    """5-minute bars from 9:30; prices: list of (open, high, low, close)."""
    t0 = pd.Timestamp(f"{date} 09:30", tz="America/New_York")
    rows = [{"timestamp": t0 + pd.Timedelta(minutes=5 * i), "date": pd.Timestamp(date), "open": o, "high": h,
             "low": l, "close": c, "volume": symbol_vol} for i, (o, h, l, c) in enumerate(prices)]
    return pd.DataFrame(rows)


def flat(n, p=100.0, wiggle=0.1):
    return [(p, p + wiggle, p - wiggle, p)] * n


def test_vwap_and_band():
    df = stretch_indicators(day_bars([(10, 12, 8, 10), (10, 13, 10, 13)]))
    assert df["vwap"].iloc[0] == pytest.approx(10)
    assert df["vwap"].iloc[1] == pytest.approx((10 + 12) / 2)
    assert df["sigma"].iloc[1] == pytest.approx(1.0)


def test_stretch_buys_the_drop_and_sells_at_vwap():
    prices = flat(12) + [(100, 100, 98.5, 98.6)]  # 10:30 bar closes far below VWAP (~99.9)
    prices += [(98.7, 99.0, 98.6, 98.9), (98.9, 100.2, 98.9, 100.0)] + flat(64)
    df = stretch_indicators(day_bars(prices))
    trades, _ = run_stretch({"X": df}, "2024-01-01", "2024-01-31", notional=1000, cost=0)
    assert len(trades) == 1
    t = trades.iloc[0]
    assert t["entry"] == 98.7 and t["exit_kind"] == "target" and t["exit"] == t["target"]
    assert t["stop"] == pytest.approx(round(98.7 - (t["target"] - 98.7), 2))
    assert t["pl"] == pytest.approx(1000 / 98.7 * (t["target"] - 98.7))


def test_stretch_stop_and_same_bar_counts_as_a_loss():
    prices = flat(12) + [(100, 100, 98.5, 98.6), (98.7, 101, 90, 99)] + flat(64, 99)
    trades, _ = run_stretch({"X": stretch_indicators(day_bars(prices))}, "2024-01-01", "2024-01-31", cost=0)
    assert trades.iloc[0]["exit_kind"] == "stop" and trades.iloc[0]["exit"] == trades.iloc[0]["stop"]


def test_stretch_time_exit_and_no_entries_before_ten():
    early = flat(3) + [(100, 100, 97, 97.1)] + flat(8, 97.1, 0.02)  # 9:45 drop: too early
    trades, _ = run_stretch({"X": stretch_indicators(day_bars(early + flat(66, 97.1, 0.02)))},
                            "2024-01-01", "2024-01-31", cost=0)
    assert trades.empty
    prices = flat(12) + [(100, 100, 98.5, 98.6)] + flat(65, 98.8, 0.05)  # never gets back to VWAP or the stop
    trades, _ = run_stretch({"X": stretch_indicators(day_bars(prices))}, "2024-01-01", "2024-01-31", cost=0)
    t = trades.iloc[0]
    assert t["exit_kind"] == "time" and t["exit_time"].strftime("%H:%M") == "15:55"
