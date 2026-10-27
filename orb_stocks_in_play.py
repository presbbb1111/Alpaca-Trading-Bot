"""Backtest the Opening Range Breakout on "stocks in play".

Rules (my reading of Zarattini, Barbon & Aziz, 2024, "A Profitable Day
Trading Strategy for the U.S. Equity Market"):
  Universe  US common stocks on NYSE, NASDAQ and AMEX, delisted ones included.
  Filters   today's open above $5, 14-day average volume of at least 1M
            shares, 14-day average true range (ATR) above $0.50.
  In play   relative volume = volume in the first 5 minutes (9:30-9:35) divided
            by the average first-5-minute volume of the previous 14 days. Take
            the 20 highest, and only those at 100% or more.
  Direction first 5-minute bar closed up: long; closed down: short; flat: skip.
  Entry     a stop order at the opening range high (long) or low (short).
            If the price never breaks it, no trade.
  Stop      10% of the 14-day ATR from the entry price.
  Exit      at the close if the stop hasn't been hit. Never held overnight.
  Size      risk 1% of the account per trade, with each trade capped at 1/20
            of 4x leverage, so the 20 trades together never exceed 4x.

Costs: a cost per share on each entry and exit (spread + slippage), plus
$0.0005/share commission. Fills are otherwise idealized: on the bar where the
entry happens, the trade counts as stopped out only if that bar closes past
the stop; later bars stop out as soon as they touch it; gaps fill at the open.

    python orb_stocks_in_play.py              # downloads data (resumable), then runs
    python orb_stocks_in_play.py --start 2024-01-01

Data comes from Alpaca's full-market (SIP) feed with your .env keys and is
cached in data/sip/. The first run makes several thousand requests.
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent
DATA = BOT_DIR / "data" / "sip"
ET = ZoneInfo("America/New_York")
HISTORY_START = datetime(2015, 11, 1)  # warm-up for the 14-day averages

MIN_PRICE = 5
MIN_AVG_VOLUME = 1_000_000
MIN_ATR = 0.50
LOOKBACK = 14
TOP_N = 20
MIN_RELATIVE_VOLUME = 1.0
STOP_ATR_FRACTION = 0.10
RISK_PER_TRADE = 0.01
MAX_LEVERAGE = 4
START_CAPITAL = 25_000
COMMISSION_PER_SHARE = 0.0005
COST_LEVELS = [0.0, 0.01, 0.02, 0.05]

# Names that mark funds, leveraged products, warrants, units, preferreds, etc.
NOT_COMMON_STOCK = re.compile(
    r"\b(ETF|ETN|Fund|iShares|ProShares|Direxion|SPDR|Invesco QQQ|Leveraged|Inverse|2X|3X|"
    r"Bull|Bear|Index|Warrants?|Rights?|Units?|Preferred|Notes|Depositary Shares,? each)\b",
    re.IGNORECASE,
)


# --- Alpaca access ---------------------------------------------------------

def keys():
    load_dotenv(BOT_DIR / ".env")
    return os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")


def data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(*keys())


def fetch(client, symbols, timeframe, start, end):
    """Bars as a flat DataFrame. Retries on rate limits and network errors."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    for attempt in range(6):
        try:
            df = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=list(symbols), timeframe=timeframe, start=start, end=end,
                feed=DataFeed.SIP, adjustment=Adjustment.RAW)).df
            return df.reset_index() if len(df) else pd.DataFrame()
        except Exception as e:
            wait = 60 if "429" in str(e) or "rate" in str(e).lower() else 10 * (attempt + 1)
            print(f"  request failed ({str(e)[:80]}), retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("Alpaca request kept failing")


def five_min():
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    return TimeFrame(5, TimeFrameUnit.Minute)


# --- Stage 1: universe ------------------------------------------------------

def universe():
    path = DATA / "universe.json"
    if path.exists():
        return json.loads(path.read_text())
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    # raw_data: the library's model rejects exchanges it doesn't know yet.
    trading = TradingClient(*keys(), paper=True, raw_data=True)
    symbols = set()
    for status in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
        for a in trading.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=status)):
            if a["exchange"] in ("NYSE", "NASDAQ", "AMEX") and not NOT_COMMON_STOCK.search(a["name"] or ""):
                if re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", a["symbol"]):
                    symbols.add(a["symbol"])
    symbols = sorted(symbols)
    DATA.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(symbols))
    return symbols


# --- Stage 2: daily bars -> which stocks pass the filters each day ----------

def daily_filters(symbols):
    """Rows (date, symbol) for every stock that passed the filters within the
    last LOOKBACK+1 days, with its ATR and whether it passes today."""
    path = DATA / "daily_filters.pkl"
    if path.exists():
        return pd.read_pickle(path)
    batch_dir = DATA / "daily"
    batch_dir.mkdir(exist_ok=True)
    client = data_client()
    from alpaca.data.timeframe import TimeFrame
    end = datetime.now(timezone.utc) - timedelta(days=1)
    batches = [symbols[i:i + 200] for i in range(0, len(symbols), 200)]
    parts = []
    for n, batch in enumerate(batches):
        out = batch_dir / f"batch_{n:03}.pkl"
        if not out.exists():
            print(f"Daily bars: batch {n + 1}/{len(batches)}")
            bars = fetch(client, batch, TimeFrame.Day, HISTORY_START, end)
            summarize_daily(bars).to_pickle(out)
        parts.append(pd.read_pickle(out))
    result = pd.concat(parts, ignore_index=True)
    result.to_pickle(path)
    return result


def summarize_daily(bars):
    if bars.empty:
        return pd.DataFrame(columns=["date", "symbol", "eligible", "atr"])
    bars = bars.sort_values(["symbol", "timestamp"])
    bars["date"] = bars.timestamp.dt.tz_convert(ET).dt.date
    g = bars.groupby("symbol", group_keys=False)
    prev_close = g.close.shift(1)
    true_range = pd.concat([bars.high - bars.low, (bars.high - prev_close).abs(), (bars.low - prev_close).abs()], axis=1).max(axis=1)
    # Averages use only the days before today, so nothing peeks ahead.
    bars["atr"] = true_range.groupby(bars.symbol).transform(lambda s: s.rolling(LOOKBACK).mean().shift(1))
    bars["avg_volume"] = g.volume.transform(lambda s: s.rolling(LOOKBACK).mean().shift(1))
    bars["eligible"] = (bars.open > MIN_PRICE) & (bars.avg_volume >= MIN_AVG_VOLUME) & (bars.atr > MIN_ATR)
    # Keep the window around eligible days: relative volume needs the
    # opening volume of the previous 14 days too.
    recent = bars.groupby("symbol").eligible.transform(lambda s: s[::-1].rolling(LOOKBACK + 1, min_periods=1).max()[::-1].astype(bool))
    return bars.loc[recent, ["date", "symbol", "eligible", "atr"]].reset_index(drop=True)


# --- Stage 3: opening 5-minute bars ----------------------------------------

def opening_bars(filters, start):
    """The 9:30-9:35 bar for every stock in the window, every day."""
    out_dir = DATA / "open"
    out_dir.mkdir(exist_ok=True)
    client = data_client()
    days = sorted(d for d in filters.date.unique() if d >= start - timedelta(days=30))
    for n, day in enumerate(days):
        out = out_dir / f"{day}.pkl"
        if out.exists():
            continue
        symbols = filters.symbol[filters.date == day].tolist()
        if n % 20 == 0:
            print(f"Opening bars: {day} ({n + 1}/{len(days)}, {len(symbols)} stocks)")
        t = datetime.combine(day, dtime(9, 30), ET)
        chunks = [fetch(client, symbols[i:i + 400], five_min(), t, t + timedelta(seconds=59))
                  for i in range(0, len(symbols), 400)]
        bars = pd.concat([c for c in chunks if len(c)], ignore_index=True) if any(len(c) for c in chunks) else pd.DataFrame()
        if len(bars):
            bars = bars[["symbol", "open", "high", "low", "close", "volume"]]
            bars["date"] = day
        bars.to_pickle(out)
    frames = [pd.read_pickle(out_dir / f"{d}.pkl") for d in days]
    return pd.concat([f for f in frames if len(f)], ignore_index=True)


# --- Stage 4: pick the stocks in play ---------------------------------------

def stocks_in_play(filters, opens, start):
    opens = opens.sort_values(["symbol", "date"])
    previous = opens.groupby("symbol").volume.transform(lambda s: s.rolling(LOOKBACK, min_periods=10).mean().shift(1))
    opens["relative_volume"] = opens.volume / previous
    opens = opens.merge(filters[filters.eligible], on=["date", "symbol"])
    opens = opens[(opens.date >= start) & (opens.relative_volume >= MIN_RELATIVE_VOLUME) & (opens.close != opens.open)]
    picks = opens.sort_values("relative_volume", ascending=False).groupby("date").head(TOP_N)
    return picks.sort_values(["date", "relative_volume"], ascending=[True, False]).reset_index(drop=True)


# --- Stage 5: the rest of the day for those stocks --------------------------

def intraday_bars(picks):
    """1-minute bars from 9:35 to the close for each day's picks. The stop is
    only 10% of ATR, often smaller than one 5-minute bar's range, so 5-minute
    bars can't tell whether the entry or the stop came first."""
    from alpaca.data.timeframe import TimeFrame
    out_dir = DATA / "intraday_1min"
    out_dir.mkdir(exist_ok=True)
    client = data_client()
    days = sorted(picks.date.unique())
    for n, day in enumerate(days):
        out = out_dir / f"{day}.pkl"
        if out.exists():
            continue
        if n % 50 == 0:
            print(f"Intraday bars: {day} ({n + 1}/{len(days)})")
        symbols = picks.symbol[picks.date == day].tolist()
        bars = fetch(client, symbols, TimeFrame.Minute,
                     datetime.combine(day, dtime(9, 35), ET), datetime.combine(day, dtime(15, 59), ET))
        if len(bars):
            bars = bars[["symbol", "timestamp", "open", "high", "low", "close"]]
        bars.to_pickle(out)
    return {d: pd.read_pickle(out_dir / f"{d}.pkl") for d in days}


# --- Simulation -------------------------------------------------------------

def trade_result(pick, bars, strict=True):
    """(direction, entry, exit) for one stock-day, or None if the range never broke.

    A bar can't show whether its other extreme came before or after the entry.
    strict: the entry bar counts as stopped out if it touches the stop at all.
    Otherwise it only counts if the bar closes past the stop.
    """
    d = 1 if pick.close > pick.open else -1
    level = pick.high if d == 1 else pick.low
    stop = None
    for i, (o, h, l, c) in enumerate(zip(bars.open, bars.high, bars.low, bars.close)):
        if stop is None:
            if (h >= level) if d == 1 else (l <= level):
                entry = max(level, o) if d == 1 else min(level, o)
                stop = entry - d * STOP_ATR_FRACTION * pick.atr
                touched = (l <= stop) if d == 1 else (h >= stop)
                closed_past = (c <= stop) if d == 1 else (c >= stop)
                if closed_past or (strict and touched):
                    return d, entry, stop
            continue
        if (l <= stop) if d == 1 else (h >= stop):
            gapped = (o < stop) if d == 1 else (o > stop)
            return d, entry, (o if gapped else stop)
    if stop is None:
        return None
    return d, entry, bars.close.iloc[-1]


def precompute_trades(picks, intraday, strict):
    rows = []
    for pick in picks.itertuples():
        day = intraday.get(pick.date)
        if day is None or day.empty:
            continue
        bars = day[day.symbol == pick.symbol]
        if bars.empty:
            continue
        result = trade_result(pick, bars, strict)
        if result:
            d, entry, exit_ = result
            rows.append({"date": pick.date, "symbol": pick.symbol, "direction": d, "entry": entry,
                         "exit": exit_, "risk": STOP_ATR_FRACTION * pick.atr})
    return pd.DataFrame(rows)


def simulate(trades, cost):
    """Same $25k account every day (profits aren't reinvested), so results
    stay in a size these stocks could actually absorb."""
    capital = START_CAPITAL
    total = START_CAPITAL
    days = []
    for date, group in trades.groupby("date"):
        cap_per_trade = capital * MAX_LEVERAGE / TOP_N
        shares = np.floor(np.minimum(capital * RISK_PER_TRADE / group.risk, cap_per_trade / group.entry))
        pnl = shares * (group.exit - group.entry) * group.direction - shares * 2 * (cost + COMMISSION_PER_SHARE)
        total += pnl.sum()
        days.append({"date": pd.Timestamp(date), "pnl": pnl.sum(), "equity": total,
                     "trades": int((shares > 0).sum()), "wins": int((pnl > 0).sum())})
    return pd.DataFrame(days).set_index("date")


def benchmark(start):
    import yfinance as yf
    closes = yf.Ticker("SPY").history(start=str(start))["Close"]
    return closes


def report(trade_sets, start, end_note):
    """trade_sets: {"strict": trades, "lenient": trades} for the two entry-bar rules."""
    spy = benchmark(start)
    years = (spy.index[-1] - spy.index[0]).days / 365.25
    spy_year = (spy.iloc[-1] / spy.iloc[0]) ** (1 / years) - 1
    spy_drop = (spy / spy.cummax() - 1).min()
    any_trades = next(iter(trade_sets.values()))

    lines = [f"Stocks-in-play ORB, 1-minute bars: {any_trades.date.min()} to {any_trades.date.max()} ({years:.1f} years)",
             end_note,
             f"Fixed ${START_CAPITAL:,} account every day (profits not reinvested). Returns are % of ${START_CAPITAL:,} per year.",
             "",
             f"{'Entry-bar rule':<16}{'Cost/share/side':>16}{'Avg per year':>14}{'Worst drop':>12}{'Sharpe':>8}{'Trades':>9}{'Wins':>7}{'Avg R':>7}",
             "-" * 89]
    yearly = {}
    for rule, trades in trade_sets.items():
        gross_r = ((trades.exit - trades.entry) * trades.direction / trades.risk).mean()
        for cost in COST_LEVELS:
            days = simulate(trades, cost)
            daily = days.pnl / START_CAPITAL
            equity = START_CAPITAL + days.pnl.cumsum()
            peak = pd.concat([pd.Series([START_CAPITAL]), equity]).cummax().iloc[1:].values
            worst = ((equity - peak) / START_CAPITAL).min()
            if cost == 0.01:
                yearly[rule] = days
            lines.append(f"{rule:<16}{'$' + format(cost, '.3f'):>16}{days.pnl.sum() / START_CAPITAL / years:>14.1%}{worst:>12.0%}"
                         f"{daily.mean() / daily.std() * np.sqrt(252):>8.2f}{days.trades.sum():>9,}"
                         f"{days.wins.sum() / max(days.trades.sum(), 1):>7.0%}{gross_r if cost == 0 else float('nan'):>7.2f}")
        lines.append("")
    lines.append(f"{'SPY buy and hold':<32}{spy_year:>14.1%}{spy_drop:>12.0%}")

    lines += ["", "Year by year at $0.01 per share per side (% of $25k):",
              f"{'Year':<8}{'Strict':>9}{'Lenient':>9}{'SPY':>9}"]
    for year in sorted(set(yearly["strict"].index.year)):
        s_ = spy[spy.index.year == year]
        prev = spy[spy.index.year < year]
        spy_ret = s_.iloc[-1] / (prev.iloc[-1] if len(prev) else s_.iloc[0]) - 1
        vals = [yearly[r][yearly[r].index.year == year].pnl.sum() / START_CAPITAL for r in ("strict", "lenient")]
        lines.append(f"{year:<8}{vals[0]:>9.1%}{vals[1]:>9.1%}{spy_ret:>+9.1%}")

    lines += [
        "",
        "Entry-bar rule   a 1-minute bar can't show whether the price hit the entry or the stop first.",
        "                 strict: touching the stop in the entry minute counts as stopped out.",
        "                 lenient: only if that minute also closes past the stop.",
        "                 The truth is somewhere in between; a big gap between the two means the",
        "                 result depends on fills a backtest can't see.",
        "Cost/share/side  spread + slippage on entry and again on exit, plus $0.0005/share commission.",
        "                 $0 matches the paper's assumptions. Stocks in play often have wider spreads",
        "                 than 1 cent at the open, so 2-5 cents is a realistic range for many of them.",
        "Avg R            average gross trade as a multiple of the risk (entry-to-stop distance), before costs.",
        "Wins             share of trades that made money after costs.",
        "Past results do not predict future results.",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2016-01-01")
    args = parser.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()

    symbols = universe()
    print(f"Universe: {len(symbols):,} stocks (active and delisted)")
    filters = daily_filters(symbols)
    per_day = filters[filters.eligible].groupby("date").size()
    print(f"Stocks passing the filters: about {per_day[per_day.index >= start].median():.0f} per day")
    opens = opening_bars(filters, start)
    picks = stocks_in_play(filters, opens, start)
    intraday = intraday_bars(picks)
    trade_sets = {rule: precompute_trades(picks, intraday, strict=(rule == "strict")) for rule in ("strict", "lenient")}
    text = report(trade_sets, start, f"Universe {len(symbols):,} stocks, delisted included; about {per_day.median():.0f} pass the filters on a typical day.")
    print(text)
    (BOT_DIR / "results" / "orb_stocks_in_play_results.txt").write_text(text + "\n")


if __name__ == "__main__":
    main()
