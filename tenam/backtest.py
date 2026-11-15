"""Run the 10 AM model over a date range, apply costs and sizing, write trades.

    python backtest.py dev                 development period, QQQ and SPY
    python backtest.py holdout             the holdout (Phase 3: run once)
    python backtest.py 2021-01-01 2021-12-31 --slip 0.02

Writes results/trades_{period}_{symbol}.csv and results/days_{period}_{symbol}.csv.
"""

import argparse
import math
from dataclasses import asdict

import pandas as pd

import config
from data import HERE, load_bars, load_calendar
from strategy import Params, median_widths, r_with_costs, run_day

RESULTS = HERE / "results"


def period_dates(name):
    if name == "dev":
        return pd.Timestamp(config.DEV_START), pd.Timestamp(config.DEV_END)
    if name == "holdout":
        return pd.Timestamp(config.HOLDOUT_START), None
    raise ValueError(name)


def run(symbol, start, end=None, params=None, slip=config.SLIP_PER_SHARE, bars=None):
    """Returns (trades, days). end=None means through the latest cached day."""
    params = params or Params()
    bars = load_bars(symbol) if bars is None else bars
    cal = load_calendar().set_index("date")
    widths = median_widths(bars, or_end=params.or_end)
    keep = bars["date"] >= start
    if end is not None:
        keep &= bars["date"] <= end
    trades, days = [], []
    for day, g in bars[keep].groupby("date", sort=True):
        med = widths.get(day)
        res = run_day(g, None if pd.isna(med) else med, cal.loc[day, "close"].time(), params)
        days.append({"date": day, "skipped": res.skipped, "trades": len(res.trades),
                     "or_width": res.or_width, "median_width": res.median_width,
                     "early_close": cal.loc[day, "close"].hour < 16})
        for t in res.trades:
            row = asdict(t)
            row.update(date=day, symbol=symbol, gross_r=t.gross_r, r=r_with_costs(t, slip))
            trades.append(row)
    cols = ["date", "symbol", "side", "trade_no", "break_time", "retest_time", "entry_time", "entry",
            "stop", "target", "risk", "stop_floored", "exit_time", "exit_price", "exit_kind", "gross_r", "r"]
    return pd.DataFrame(trades, columns=cols), pd.DataFrame(days)


def size(trades, start_equity, risk_pct=config.RISK_PCT, slip=config.SLIP_PER_SHARE,
         commission=config.COMMISSION):
    """Compound 1% risk per trade, capped at 100% of equity in notional (no leverage).

    Adds shares, capped, pnl and equity columns to a copy of trades.
    """
    equity = start_equity
    out = trades.copy()
    shares, capped, pnl, eq = [], [], [], []
    for t in trades.itertuples():
        want = math.floor(equity * risk_pct / t.risk)
        most = math.floor(equity / t.entry)
        n = max(min(want, most), 0)
        sign = 1 if t.side == "long" else -1
        entry_fill = t.entry + sign * slip
        exit_fill = t.exit_price - sign * slip
        p = n * sign * (exit_fill - entry_fill) - (2 * commission if n else 0)
        equity += p
        shares.append(n)
        capped.append(want > most)
        pnl.append(p)
        eq.append(equity)
    out["shares"], out["capped"], out["pnl"], out["equity"] = shares, capped, pnl, eq
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("period", help="dev, holdout, or a start date")
    parser.add_argument("end", nargs="?")
    parser.add_argument("--slip", type=float, default=config.SLIP_PER_SHARE)
    parser.add_argument("--tag", default="")
    parser.add_argument("--min-stop-pct", type=float, default=config.MIN_STOP_PCT)
    parser.add_argument("--min-stop-or", type=float, default=config.MIN_STOP_OR_FRAC)
    args = parser.parse_args()
    if args.period in ("dev", "holdout"):
        start, end = period_dates(args.period)
        name = args.period
    else:
        start, end = pd.Timestamp(args.period), pd.Timestamp(args.end) if args.end else None
        name = f"{start:%Y%m%d}-{end:%Y%m%d}" if end is not None else f"{start:%Y%m%d}-"
    name += args.tag
    RESULTS.mkdir(exist_ok=True)
    for symbol in config.SYMBOLS:
        params = Params(min_stop_pct=args.min_stop_pct, min_stop_or_frac=args.min_stop_or)
        trades, days = run(symbol, start, end, params=params, slip=args.slip)
        trades.to_csv(RESULTS / f"trades_{name}_{symbol}.csv", index=False)
        days.to_csv(RESULTS / f"days_{name}_{symbol}.csv", index=False)
        print(f"{symbol}: {len(trades)} trades on {len(days)} days -> results/trades_{name}_{symbol}.csv")


if __name__ == "__main__":
    main()
