"""Backtest the 5-minute Opening Range Breakout (ORB) day-trading strategy.

Rules (Zarattini & Aziz, 2023, "Can Day Trading Really Be Profitable?"):
  - Look at the first 5-minute bar of the day (9:30-9:35 ET).
  - If it closed up, go long at 9:35; if it closed down, go short. Flat bar: no trade.
  - Stop loss at the other end of that first bar (its low for longs, high for shorts).
  - Profit target at 10x the risk (entry-to-stop distance).
  - Anything still open is closed at the end of the day. Never held overnight.
  - Size: risk 1% of the account per trade, capped at 4x leverage.

The paper's results left out the bid-ask spread and slippage. This backtest
charges a cost per share on every entry and exit so you can see how much of
the result survives them.

    python orb_backtest.py                       # QQQ, 2016 to today
    python orb_backtest.py --symbol SPY
    python orb_backtest.py --start 2024-01-01    # only after the paper came out

Price bars come from Alpaca (your .env keys) and are cached in data/.
"""

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent
DATA_DIR = BOT_DIR / "data"
MARKET_TZ = "America/New_York"

START_CAPITAL = 25_000
RISK_PER_TRADE = 0.01
TARGET_R = 10
COMMISSION_PER_SHARE = 0.0005  # what the paper charged

# Extra cost per share on each side of a trade (entry and exit), covering the
# bid-ask spread plus slippage. QQQ's spread is usually about 1 cent.
COST_LEVELS = [0.0, 0.005, 0.01, 0.02]
LEVERAGE_LEVELS = [4, 1]


# --- Data -------------------------------------------------------------------

def load_bars(symbol, start):
    """5-minute regular-session bars, cached per symbol in data/."""
    DATA_DIR.mkdir(exist_ok=True)
    cache = DATA_DIR / f"{symbol}_5min.csv.gz"
    if cache.exists():
        bars = pd.read_csv(cache, index_col=0, parse_dates=True)
        bars.index = pd.to_datetime(bars.index, utc=True).tz_convert(MARKET_TZ)
        fetch_from = bars.index[-1].tz_convert("UTC").tz_localize(None) + timedelta(minutes=5)
    else:
        bars = None
        fetch_from = datetime(2016, 1, 1)

    new = fetch_bars(symbol, fetch_from)
    if new is not None and len(new):
        bars = new if bars is None else pd.concat([bars, new[~new.index.isin(bars.index)]])
        bars.to_csv(cache)

    bars = bars[bars.index >= pd.Timestamp(start, tz=MARKET_TZ)]
    times = bars.index.time
    return bars[(times >= pd.Timestamp("09:30").time()) & (times < pd.Timestamp("16:00").time())]


def fetch_bars(symbol, start):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    load_dotenv(BOT_DIR / ".env")
    client = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
    # The free data plan can't see the most recent 15 minutes.
    end = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=20)
    chunks = []
    while start < end:
        chunk_end = min(start + timedelta(days=365), end)
        print(f"Downloading {symbol} 5-minute bars {start:%Y-%m-%d} to {chunk_end:%Y-%m-%d}...")
        df = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start, end=chunk_end)).df
        if len(df):
            chunks.append(df.droplevel("symbol")[["open", "high", "low", "close", "volume"]])
        start = chunk_end
    if not chunks:
        return None
    bars = pd.concat(chunks)
    bars.index = bars.index.tz_convert(MARKET_TZ)
    return bars[~bars.index.duplicated()]


# --- Strategy ---------------------------------------------------------------

def day_signals(bars):
    """One row per day: direction, entry, stop, target and the rest of the day's bars."""
    days = []
    for date, day in bars.groupby(bars.index.date):
        if len(day) < 3 or day.index[0].time() != pd.Timestamp("09:30").time():
            continue  # missing the opening bar
        first, rest = day.iloc[0], day.iloc[1:]
        if first.close > first.open:
            direction, stop = 1, first.low
        elif first.close < first.open:
            direction, stop = -1, first.high
        else:
            continue
        entry = rest.iloc[0].open
        risk = (entry - stop) * direction
        if risk <= 0:
            continue  # gapped past the stop before entry
        days.append({
            "date": pd.Timestamp(date), "direction": direction, "entry": entry, "stop": stop,
            "target": entry + direction * TARGET_R * risk, "risk": risk,
            "high": rest.high.values, "low": rest.low.values, "open": rest.open.values,
            "last_close": rest.iloc[-1].close,
        })
    return days


def exit_price(day):
    """Walk the bars after entry; the stop wins if stop and target hit in the same bar."""
    d = day["direction"]
    for i, (o, h, l) in enumerate(zip(day["open"], day["high"], day["low"])):
        stop_hit = l <= day["stop"] if d == 1 else h >= day["stop"]
        target_hit = h >= day["target"] if d == 1 else l <= day["target"]
        if stop_hit:
            # If the bar opened past the stop (a gap), we get the open, not the stop.
            gapped = (o < day["stop"]) if d == 1 else (o > day["stop"])
            return (o if gapped and i > 0 else day["stop"]), "stop"
        if target_hit:
            return day["target"], "target"
    return day["last_close"], "close"


def simulate(days, cost_per_share, max_leverage):
    capital = START_CAPITAL
    rows = []
    for day in days:
        shares = int(min(capital * RISK_PER_TRADE / day["risk"], capital * max_leverage / day["entry"]))
        if shares < 1:
            continue
        exit_at, reason = exit_price(day)
        gross = shares * (exit_at - day["entry"]) * day["direction"]
        costs = shares * 2 * (cost_per_share + COMMISSION_PER_SHARE)
        capital += gross - costs
        rows.append({"date": day["date"], "pnl": gross - costs, "equity": capital, "reason": reason})
    return pd.DataFrame(rows).set_index("date")


def stats(trades, buy_hold_years):
    equity = pd.concat([pd.Series([START_CAPITAL]), trades.equity.reset_index(drop=True)])
    years = buy_hold_years
    daily = trades.equity.pct_change().fillna(trades.equity.iloc[0] / START_CAPITAL - 1)
    return {
        "final": trades.equity.iloc[-1],
        "per_year": (trades.equity.iloc[-1] / START_CAPITAL) ** (1 / years) - 1,
        "worst_drop": (equity / equity.cummax() - 1).min(),
        "sharpe": daily.mean() / daily.std() * np.sqrt(252) if daily.std() else float("nan"),
        "trades": len(trades),
        "win_rate": (trades.pnl > 0).mean(),
        "targets": (trades.reason == "target").mean(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="QQQ")
    parser.add_argument("--start", default="2016-01-01")
    args = parser.parse_args()

    bars = load_bars(args.symbol, args.start)
    days = day_signals(bars)
    first, last = bars.index[0], bars.index[-1]
    years = (last - first).days / 365.25

    # Buy and hold over the same period, for comparison.
    daily_close = bars.close.groupby(bars.index.date).last()
    hold_final = START_CAPITAL * daily_close.iloc[-1] / bars.open.iloc[0]
    hold_equity = START_CAPITAL * daily_close / bars.open.iloc[0]
    hold_drop = (hold_equity / hold_equity.cummax() - 1).min()

    lines = [
        f"ORB backtest on {args.symbol}: {first.date()} to {last.date()} ({years:.1f} years), "
        f"${START_CAPITAL:,} start, {len(days)} trade days",
        "",
        f"{'Leverage cap':<14}{'Cost/share/side':>17}{'$25k becomes':>15}{'Per year':>10}{'Worst drop':>12}{'Sharpe':>8}{'Wins':>7}{'Hit 10R':>9}",
        "-" * 92,
    ]
    results = {}
    for lev in LEVERAGE_LEVELS:
        for cost in COST_LEVELS:
            trades = simulate(days, cost, lev)
            s = stats(trades, years)
            results[(lev, cost)] = trades
            lines.append(
                f"{str(lev) + 'x':<14}{'$' + format(cost, '.3f'):>17}{'$' + format(s['final'], ',.0f'):>15}"
                f"{s['per_year']:>10.1%}{s['worst_drop']:>12.0%}{s['sharpe']:>8.2f}{s['win_rate']:>7.0%}{s['targets']:>9.1%}"
            )
    lines.append(
        f"{'Buy and hold':<14}{'':>17}{'$' + format(hold_final, ',.0f'):>15}"
        f"{(hold_final / START_CAPITAL) ** (1 / years) - 1:>10.1%}{hold_drop:>12.0%}"
    )

    # Year by year, at the paper's 4x leverage with a realistic 1-cent cost.
    trades = results[(4, 0.01)]
    lines += ["", "Year by year (4x leverage, $0.01 cost per share per side):",
              f"{'Year':<8}{'Return':>9}{'Trades':>8}{'Wins':>7}   {args.symbol} buy and hold"]
    for year, group in trades.groupby(trades.index.year):
        start_equity = group.equity.iloc[0] - group.pnl.iloc[0]
        year_closes = daily_close[pd.to_datetime(daily_close.index).year == year]
        prev = daily_close[pd.to_datetime(daily_close.index).year < year]
        hold_start = prev.iloc[-1] if len(prev) else bars.open.iloc[0]
        lines.append(f"{year:<8}{group.equity.iloc[-1] / start_equity - 1:>9.1%}{len(group):>8}"
                     f"{(group.pnl > 0).mean():>7.0%}   {year_closes.iloc[-1] / hold_start - 1:>+.1%}")

    lines += [
        "",
        "Cost/share/side  spread + slippage charged on entry and again on exit, on top of the",
        "                 paper's $0.0005/share commission. $0 reproduces the paper's assumptions.",
        "Worst drop       biggest fall from a high point. Sharpe: return per unit of volatility (above 1 is good).",
        "Hit 10R          share of trades that reached the profit target instead of the stop or the close.",
        "Fills are idealized: stops fill at the stop price unless the bar gapped past it.",
        "Past results do not predict future results.",
    ]
    report = "\n".join(lines)
    print(report)
    (BOT_DIR / "results" / f"orb_results_{args.symbol}.txt").write_text(report + "\n")


if __name__ == "__main__":
    main()
