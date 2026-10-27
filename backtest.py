"""Compare trading strategies on past prices.

    python backtest.py                 # every stock in config.py, last 10 years
    python backtest.py --years 5
    python backtest.py --symbol BTC-USD   # any Yahoo Finance symbol, crypto too

Each strategy is either fully invested or fully in cash. A signal at a day's
close is traded at the next day's open, like the live bot. Results are also
saved to results/backtest_results.txt.
"""

import argparse
from pathlib import Path

import pandas as pd
import yfinance as yf

import config
import strategies

BOT_DIR = Path(__file__).resolve().parent

def buy_and_hold(close, next_day=None):
    return pd.Series(1, index=close.index)


STRATEGIES = {"Buy and hold": buy_and_hold}
STRATEGIES.update({name: fn for name, fn in strategies.STRATEGIES.values()})


# --- Simulation -------------------------------------------------------------

def simulate(prices, signal, start, cost):
    """Return the daily account value (starting at 1.0) and the list of trades."""
    # Return from one day's open to the next day's open.
    open_to_open = prices["Open"].shift(-1) / prices["Open"] - 1
    # A signal at day t's close is traded at day t+1's open.
    position = signal.shift(1).fillna(0)

    position, open_to_open = position[start:], open_to_open[start:].fillna(0)
    trades_made = position.diff().abs().fillna(position.iloc[0])
    daily = position * open_to_open - trades_made * cost
    equity = (1 + daily).cumprod()

    # Group consecutive invested days into round-trip trades.
    trade_id = (position.diff().fillna(position.iloc[0]) == 1).cumsum()
    trades = [
        (1 + group).prod() - 1
        for _, group in daily[position == 1].groupby(trade_id[position == 1])
    ]
    return equity, position, trades


def summarize(name, equity, position, trades, years):
    peak = equity.cummax()
    return {
        "Strategy": name,
        "Total return": equity.iloc[-1] - 1,
        "Per year": equity.iloc[-1] ** (1 / years) - 1,
        "Worst drop": (equity / peak - 1).min(),
        "Trades": len(trades),
        "Win rate": sum(t > 0 for t in trades) / len(trades) if trades else float("nan"),
        "Invested": position.mean(),
    }


def format_table(rows, starting_cash):
    header = f"{'Strategy':<26}{'$' + format(starting_cash, ',') + ' becomes':>16}{'Total':>10}{'Per year':>10}{'Worst drop':>12}{'Trades':>8}{'Wins':>7}{'Invested':>10}"
    lines = [header, "-" * len(header)]
    for r in rows:
        wins = "-" if pd.isna(r["Win rate"]) or r["Strategy"] == "Buy and hold" else f"{r['Win rate']:.0%}"
        lines.append(
            f"{r['Strategy']:<26}"
            f"{'$' + format(starting_cash * (1 + r['Total return']), ',.0f'):>16}"
            f"{r['Total return']:>10.0%}"
            f"{r['Per year']:>10.1%}"
            f"{r['Worst drop']:>12.0%}"
            f"{r['Trades']:>8}"
            f"{wins:>7}"
            f"{r['Invested']:>10.0%}"
        )
    return "\n".join(lines)


LEGEND = "\n".join([
    "Total / Per year  how much the account grew overall, and the average yearly growth",
    "Worst drop        the biggest fall from a high point before recovering",
    "Trades            completed buy-then-sell round trips (an open trade at the end counts)",
    "Wins              share of those trades that made money",
    "Invested          share of days the strategy held the asset instead of cash",
    "",
    "Past results do not predict future results.",
])


def backtest_symbol(symbol, args):
    # Extra history so every indicator is warmed up on the first test day.
    prices = yf.Ticker(symbol).history(period=f"{args.years + 2}y")
    if prices.empty:
        return f"{symbol}: no price data"
    start = prices.index[-1] - pd.DateOffset(years=args.years)
    prices_in_test = prices[start:]
    years = (prices_in_test.index[-1] - prices_in_test.index[0]).days / 365.25

    rows = []
    for name, strategy in STRATEGIES.items():
        equity, position, trades = simulate(prices, strategy(prices["Close"]), start, args.cost)
        rows.append(summarize(name, equity, position, trades, years))

    return "\n".join([
        f"{symbol} backtest: {prices_in_test.index[0].date()} to {prices_in_test.index[-1].date()} "
        f"({len(prices_in_test)} trading days), {args.cost:.2%} cost per trade",
        "",
        format_table(rows, args.cash),
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", action="append", help="symbol to test (repeatable); defaults to config.SYMBOLS")
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--cash", type=int, default=10_000, help="starting amount (default 10000)")
    parser.add_argument("--cost", type=float, default=0.0005,
                        help="slippage per buy or sell as a fraction (default 0.0005 = 0.05%%)")
    args = parser.parse_args()

    sections = []
    for symbol in args.symbol or config.SYMBOLS:
        sections.append(backtest_symbol(symbol, args))
    report = "\n\n".join(sections + [LEGEND])
    print(report)
    (BOT_DIR / "results" / "backtest_results.txt").write_text(report + "\n")


if __name__ == "__main__":
    main()
