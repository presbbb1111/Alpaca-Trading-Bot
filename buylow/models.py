"""The three "buy low, sell high" rules. The backtest and the live bot both
use these, so what's tested is what trades.

- Dip: daily. Buy oversold pullbacks in uptrends, sell on the bounce.
- Grid: a ladder of buy orders under the price on one ETF, each slice sold 1% up.
- Stretch: intraday. Buy a stock stretched far below its VWAP, sell at the VWAP.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config


# --- 1. Dip buying in uptrends ----------------------------------------------

def rsi(close, days):
    """Wilder's RSI."""
    change = close.diff()
    gain = change.clip(lower=0).ewm(alpha=1 / days, min_periods=days, adjust=False).mean()
    loss = (-change.clip(upper=0)).ewm(alpha=1 / days, min_periods=days, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def dip_indicators(df):
    """Adds the trend, RSI and exit columns to one symbol's daily bars."""
    df = df.sort_values("date").reset_index(drop=True).copy()
    df["trend"] = df["close"].rolling(config.DIP_TREND_DAYS).mean()
    df["rsi"] = rsi(df["close"], config.DIP_RSI_DAYS)
    df["exit_avg"] = df["close"].rolling(config.DIP_EXIT_AVG_DAYS).mean()
    df["buy"] = (df["close"] > df["trend"]) & (df["rsi"] < config.DIP_RSI_BELOW)
    df["bounced"] = df["close"] > df["exit_avg"]
    return df


def dip_decide(rows, holdings, slots=config.DIP_SLOTS):
    """After a close: what to sell and buy at the next open.

    rows: {symbol: that day's row from dip_indicators}
    holdings: {symbol: days held, counting this day}
    Returns (sells, buys): symbols, buys ranked most oversold first.
    """
    sells = [s for s, days in holdings.items()
             if s in rows and (rows[s]["bounced"] or days >= config.DIP_MAX_DAYS)]
    free = slots - (len(holdings) - len(sells))
    candidates = sorted((r["rsi"], s) for s, r in rows.items() if r["buy"] and s not in holdings)
    return sells, [s for _, s in candidates[:max(free, 0)]]


def run_dip(daily, start, end, cash=config.SLEEVE, cost=config.DIP_COST):
    """Simulate the dip sleeve. daily: {symbol: dip_indicators frame}.
    Returns (trades, equity) DataFrames."""
    frames = {s: d.set_index("date") for s, d in daily.items()}
    dates = sorted(set().union(*[d.index for d in frames.values()]))
    dates = [d for d in dates if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    held = {}  # symbol -> dict(shares, entry, entry_date, days)
    to_sell, to_buy = [], []
    trades, equity = [], []
    for day in dates:
        today = {s: f.loc[day] for s, f in frames.items() if day in f.index}
        for s in to_sell:  # at the open
            if s not in today:
                continue
            h = held.pop(s)
            price = today[s]["open"] * (1 - cost)
            cash += h["shares"] * price
            trades.append({"symbol": s, "entry_date": h["entry_date"], "exit_date": day, "entry": h["entry"],
                           "exit": price, "shares": h["shares"], "days": h["days"],
                           "pl": h["shares"] * (price - h["entry"]), "ret": price / h["entry"] - 1})
        # Each new position gets an equal share of the sleeve, valued at the
        # last close, but never more than the cash on hand.
        value = cash + sum(h["shares"] * frames[s].loc[:day, "close"].iloc[-1] for s, h in held.items())
        for s in to_buy:
            if s not in today or len(held) >= config.DIP_SLOTS:
                continue
            price = today[s]["open"] * (1 + cost)
            dollars = min(value / config.DIP_SLOTS, cash)
            if dollars < 1:
                continue
            held[s] = {"shares": dollars / price, "entry": price, "entry_date": day, "days": 0}
            cash -= dollars
        for h in held.values():
            h["days"] += 1
        to_sell, to_buy = dip_decide(today, {s: h["days"] for s, h in held.items()})
        value = cash + sum(h["shares"] * today[s]["close"] for s, h in held.items() if s in today)
        equity.append({"date": day, "equity": value, "invested": len(held)})
    return pd.DataFrame(trades), pd.DataFrame(equity)


# --- 2. Grid ----------------------------------------------------------------

TICK = 0.01


@dataclass
class Slice:
    n: int  # 1 = the first step below the anchor
    qty: float = 0.0
    buy_price: float = 0.0
    bought_at: object = None

    @property
    def held(self):
        return self.qty > 0


@dataclass
class Grid:
    """A ladder of GRID_SLICES buy orders, GRID_STEP apart below the anchor.
    Each filled slice gets a sell order GRID_TAKE above its own buy price."""
    anchor: float
    cash: float = config.SLEEVE
    slice_dollars: float = config.SLEEVE / config.GRID_SLICES
    slices: list = field(default_factory=lambda: [Slice(n) for n in range(1, config.GRID_SLICES + 1)])
    high_since_empty: float = 0.0
    trades: list = field(default_factory=list)

    def buy_level(self, s):
        return round(self.anchor * (1 - s.n * config.GRID_STEP), 2)

    def sell_level(self, s):
        return round(s.buy_price * (1 + config.GRID_TAKE), 2)

    def empty(self):
        return not any(s.held for s in self.slices)

    def orders(self):
        """The orders that should be working: (side, slice, price)."""
        return [("sell", s, self.sell_level(s)) if s.held else ("buy", s, self.buy_level(s)) for s in self.slices]

    def on_bar(self, bar):
        """Fill limit orders the bar traded through (by at least a cent),
        sells first. A slice bought in this bar can't sell in it."""
        for s in self.slices:
            if s.held:
                price = self.sell_level(s)
                if bar["high"] >= price + TICK:
                    fill = max(bar["open"], price)
                    self.cash += s.qty * fill
                    self.trades.append({"slice": s.n, "entry_time": s.bought_at, "exit_time": bar["timestamp"],
                                        "entry": s.buy_price, "exit": fill, "qty": s.qty,
                                        "pl": s.qty * (fill - s.buy_price), "ret": fill / s.buy_price - 1})
                    s.qty, s.buy_price, s.bought_at = 0.0, 0.0, None
        for s in self.slices:
            if not s.held:
                price = self.buy_level(s)
                if bar["low"] <= price - TICK and self.cash >= self.slice_dollars - 0.01:
                    fill = min(bar["open"], price)
                    s.qty, s.buy_price, s.bought_at = self.slice_dollars / fill, fill, bar["timestamp"]
                    self.cash -= self.slice_dollars
        self.reanchor(bar["high"])

    def reanchor(self, high):
        """While nothing is held, move the ladder up behind the price."""
        if not self.empty():
            self.high_since_empty = 0.0
            return False
        self.high_since_empty = max(self.high_since_empty, high)
        if self.high_since_empty >= self.anchor * (1 + config.GRID_REANCHOR):
            self.anchor = self.high_since_empty
            return True
        return False

    def value(self, price):
        return self.cash + sum(s.qty for s in self.slices) * price


def run_grid(bars, start, end):
    """Simulate the grid on 1-minute bars. Returns (trades, daily equity)."""
    bars = bars[(bars["date"] >= pd.Timestamp(start)) & (bars["date"] <= pd.Timestamp(end))]
    grid = None
    equity = []
    for day, g in bars.groupby("date", sort=True):
        recs = g.to_dict("records")
        if grid is None:
            grid = Grid(anchor=recs[0]["open"])
        for bar in recs:
            grid.on_bar(bar)
        equity.append({"date": day, "equity": grid.value(recs[-1]["close"]),
                       "invested": sum(s.held for s in grid.slices)})
    trades = pd.DataFrame(grid.trades if grid else [])
    open_slices = [s for s in grid.slices if s.held] if grid else []
    return trades, pd.DataFrame(equity), open_slices


# --- 3. Intraday stretch below VWAP -----------------------------------------

def hhmm_minutes(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def stretch_indicators(df):
    """Adds the session VWAP, its volume-weighted standard deviation and how
    many deviations the close is from it, per day."""
    df = df.sort_values("timestamp").reset_index(drop=True).copy()
    tp = (df["high"] + df["low"] + df["close"]) / 3
    v = df["volume"].astype(float)
    g = df["date"]
    cum_v = v.groupby(g).cumsum()
    vwap = (tp * v).groupby(g).cumsum() / cum_v
    var = (tp * tp * v).groupby(g).cumsum() / cum_v - vwap * vwap
    df["vwap"] = vwap
    df["sigma"] = np.sqrt(var.clip(lower=0))
    df["z"] = (df["close"] - df["vwap"]) / df["sigma"].replace(0, np.nan)
    start = df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute
    df["closes_at"] = start + config.STRETCH_BAR_MINUTES  # minutes after midnight
    df["start_min"] = start
    return df


MIN_TARGET = 0.0025  # skip setups whose VWAP target is under 0.25% away: costs eat them


def stretch_signal(row):
    """True when this bar's close is stretched far enough below VWAP, inside
    the entry window."""
    return (hhmm_minutes(config.STRETCH_FIRST_ENTRY) <= row["closes_at"] <= hhmm_minutes(config.STRETCH_LAST_ENTRY)
            and row["z"] < -config.STRETCH_BANDS
            and (row["vwap"] - row["close"]) / row["close"] >= MIN_TARGET)


@dataclass
class StretchTrade:
    symbol: str
    signal_time: object
    target: float
    entry: float = 0.0
    stop: float = 0.0
    entry_time: object = None


def stretch_levels(entry, target):
    """Stop the same distance below the entry as the target is above it."""
    return round(entry - config.STRETCH_STOP_MULT * (target - entry), 2)


def run_stretch(frames, start, end, notional=config.SLEEVE, cost=config.STRETCH_COST):
    """Simulate one position at a time across the list on 5-minute bars. Each
    trade puts in the whole sleeve as it stands (starting at `notional`).
    frames: {symbol: stretch_indicators frame}. Returns (trades, daily equity)."""
    allbars = pd.concat([f.assign(symbol=s) for s, f in frames.items()], ignore_index=True)
    allbars = allbars[(allbars["date"] >= pd.Timestamp(start)) & (allbars["date"] <= pd.Timestamp(end))]
    flat_min = hhmm_minutes(config.STRETCH_FLAT)
    trades, equity, total = [], [], notional
    for day, d in allbars.groupby("date", sort=True):
        by_time = {t: g for t, g in d.groupby("timestamp", sort=True)}
        pending, pos, done = None, None, set()
        for t, g in by_time.items():
            rows = {r["symbol"]: r for r in g.to_dict("records")}
            if pending is not None:  # enter at this bar's open
                r = rows.get(pending.symbol)
                if r is not None and r["start_min"] < flat_min:
                    entry = r["open"]
                    if entry < pending.target * (1 - MIN_TARGET / 2):
                        pending.entry, pending.entry_time = entry, t
                        pending.stop = stretch_levels(entry, pending.target)
                        pos = pending
                    done.add(pending.symbol)
                pending = None
            if pos is not None:
                r = rows.get(pos.symbol)
                if r is not None:
                    exit_price = kind = None
                    if r["start_min"] >= flat_min:
                        exit_price, kind = r["open"], "time"
                    elif r["low"] <= pos.stop:
                        exit_price, kind = min(r["open"], pos.stop), "stop"
                    elif r["high"] >= pos.target:  # the stop is checked first: same-bar ties count as losses
                        exit_price, kind = max(r["open"], pos.target), "target"
                    if exit_price is not None:
                        buy, sell = pos.entry * (1 + cost), exit_price * (1 - cost)
                        pl = total / buy * (sell - buy)
                        total += pl
                        trades.append({"symbol": pos.symbol, "date": day, "signal_time": pos.signal_time,
                                       "entry_time": pos.entry_time, "exit_time": t, "entry": pos.entry,
                                       "target": pos.target, "stop": pos.stop, "exit": exit_price, "exit_kind": kind,
                                       "pl": pl, "ret": sell / buy - 1})
                        pos = None
                continue
            if pending is None:
                cands = [r for s, r in rows.items() if s not in done and stretch_signal(r)]
                if cands:
                    best = min(cands, key=lambda r: r["z"])
                    pending = StretchTrade(best["symbol"], t, round(best["vwap"], 2))
        if pos is not None:  # no 15:55 bar for this symbol: close at its last bar
            last = d[d["symbol"] == pos.symbol].iloc[-1]
            buy, sell = pos.entry * (1 + cost), last["close"] * (1 - cost)
            pl = total / buy * (sell - buy)
            total += pl
            trades.append({"symbol": pos.symbol, "date": day, "signal_time": pos.signal_time,
                           "entry_time": pos.entry_time, "exit_time": last["timestamp"], "entry": pos.entry,
                           "target": pos.target, "stop": pos.stop, "exit": last["close"], "exit_kind": "time",
                           "pl": pl, "ret": sell / buy - 1})
        equity.append({"date": day, "equity": total})
    return pd.DataFrame(trades), pd.DataFrame(equity)
