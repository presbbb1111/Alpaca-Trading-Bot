"""Stats and charts for a backtest run.

    python report.py dev        results/report_dev.md and results/equity_dev.png

Reads results/trades_{period}_{symbol}.csv and days_... written by backtest.py.
"""

import argparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
from backtest import RESULTS, size  # noqa: E402

COLORS = {"QQQ": "#2f6fd6", "SPY": "#d9822b"}
INK, MUTED, GRID = "#1d1d1f", "#6e6e73", "#e5e5ea"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]

CAVEATS = """\
- QQQ and SPY stand in for NQ and ES futures, which Alpaca doesn't offer. The video most likely trades
  futures, with different hours, tick sizes and leverage.
- A 1-minute backtest can't see inside a bar. When a bar touches both the stop and the target, it counts as a loss.
- Paper fills are kinder than real fills, especially on fast moves.
- Data: Alpaca SIP 1-minute bars, regular hours, New York time.
- This is research, not financial advice."""


def load(period, symbol):
    trades = pd.read_csv(RESULTS / f"trades_{period}_{symbol}.csv", parse_dates=["date"])
    days = pd.read_csv(RESULTS / f"days_{period}_{symbol}.csv", parse_dates=["date"])
    return trades, days


def max_drawdown(curve, start=0.0):
    """Largest peak-to-trough fall of a running total (start included as the first peak)."""
    values = np.concatenate([[start], np.asarray(curve, dtype=float)])
    peak = np.maximum.accumulate(values)
    return float((values - peak).min())


def longest_streak(mask):
    best = run = 0
    for m in mask:
        run = run + 1 if m else 0
        best = max(best, run)
    return best


def group_table(trades, key, order=None):
    g = trades.groupby(key)["r"]
    t = pd.DataFrame({"trades": g.size(), "win rate": g.apply(lambda r: (r > 0).mean()),
                      "avg R": g.mean(), "total R": g.sum()})
    if order is not None:
        t = t.reindex([o for o in order if o in t.index])
    return t


def stats(trades, days):
    r = trades["r"]
    wins, losses = r[r > 0], r[r <= 0]
    by_day = trades.groupby("date")["r"].sum()
    by_week = trades.groupby(trades["date"].dt.to_period("W-FRI"))["r"].sum()
    by_month = trades.groupby(trades["date"].dt.to_period("M"))["r"].sum()
    by_year = trades.groupby(trades["date"].dt.year)["r"].sum()
    best_month, best_year = by_month.idxmax(), by_year.idxmax()
    no_best_month = trades[trades["date"].dt.to_period("M") != best_month]["r"]
    no_best_year = trades[trades["date"].dt.year != best_year]["r"]
    avg_win, avg_loss = wins.mean(), losses.mean()
    return {
        "trading days": len(days),
        "days skipped (range > 2x median)": int(days["skipped"].fillna("").str.startswith("range").sum()),
        "trades": len(trades),
        "trades per day": len(trades) / len(days),
        "days with a trade": (days["trades"] > 0).mean(),
        "win rate": (r > 0).mean(),
        "breakeven win rate (at these avg win/loss)": -avg_loss / (avg_win - avg_loss),
        "avg win R": avg_win,
        "avg loss R": avg_loss,
        "expectancy R": r.mean(),
        "expectancy R before slippage": trades["gross_r"].mean(),
        "total R": r.sum(),
        "profit factor": wins.sum() / -losses.sum(),
        "max drawdown R": max_drawdown(r.cumsum()),
        "longest losing streak": longest_streak(r <= 0),
        "worst day": f"{by_day.min():+.1f}R ({by_day.idxmin():%Y-%m-%d})",
        "worst week": f"{by_week.min():+.1f}R (week ending {by_week.idxmin().end_time:%Y-%m-%d})",
        "worst month": f"{by_month.min():+.1f}R ({by_month.idxmin()})",
        "best month": f"{by_month.max():+.1f}R ({best_month})",
        "best year": f"{by_year.max():+.1f}R ({best_year})",
        "total R without best month": no_best_month.sum(),
        "expectancy without best month": no_best_month.mean(),
        "total R without best year": no_best_year.sum(),
        "expectancy without best year": no_best_year.mean(),
        "stop at the 0.05% minimum": trades["stop_floored"].mean(),
        "median stop distance": f"${trades['risk'].median():.3f} ({(trades['risk'] / trades['entry']).median():.3%})",
        "exits at target / stop / time": " / ".join(
            f"{(trades['exit_kind'] == k).mean():.0%}" for k in ("target", "stop", "time")),
    }


def dollar_stats(trades, start):
    sized = size(trades, start)
    eq = sized["equity"]
    dd = max_drawdown(eq, start)
    peak_values = np.maximum.accumulate(np.concatenate([[start], eq.to_numpy()]))
    dd_pct = ((np.concatenate([[start], eq.to_numpy()]) - peak_values) / peak_values).min()
    years = (trades["date"].iloc[-1] - trades["date"].iloc[0]).days / 365.25
    final = eq.iloc[-1]
    return {
        "start": f"${start:,.0f}",
        "end": f"${final:,.0f}",
        "return": f"{final / start - 1:+.1%}",
        "per year": f"{(final / start) ** (1 / years) - 1:+.1%}" if final > 0 else "n/a",
        "max drawdown": f"${dd:,.0f} ({dd_pct:.1%})",
        "trades where the no-leverage cap cut the size": f"{sized['capped'].mean():.0%}",
        "average risk actually taken": f"{(sized['shares'] * sized['risk'] / (sized['equity'] - sized['pnl'])).mean():.2%} of equity",
    }


def fmt(v):
    if isinstance(v, (float, np.floating)):
        return f"{v:.3f}" if abs(v) < 10 else f"{v:,.1f}"
    return str(v)


def md_table(df, pct_cols=(), index_name=""):
    cols = list(df.columns)
    lines = ["| " + " | ".join([index_name] + cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for idx, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            if c == "trades":
                cells.append(f"{int(v):,}")
            elif c in pct_cols:
                cells.append(f"{v:.1%}")
            elif isinstance(v, (float, np.floating)):
                cells.append(f"{v:+.3f}" if "avg" in c else f"{v:+.1f}" if "total" in c else f"{v:.3f}")
            else:
                cells.append(str(int(v)) if isinstance(v, (np.integer, int)) else str(v))
        lines.append("| " + " | ".join([str(idx)] + cells) + " |")
    return "\n".join(lines)


PCT_KEYS = {"days with a trade", "win rate", "breakeven win rate (at these avg win/loss)", "stop at the 0.05% minimum"}


def side_by_side(all_stats):
    keys = list(next(iter(all_stats.values())).keys())
    lines = ["| | " + " | ".join(all_stats) + " |", "|---|" + "---|" * len(all_stats)]
    for k in keys:
        cells = [f"{s[k]:.1%}" if k in PCT_KEYS else fmt(s[k]) for s in all_stats.values()]
        lines.append(f"| {k} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def equity_chart(by_symbol, title, path):
    fig, ax = plt.subplots(figsize=(11, 5.2), dpi=120)
    for symbol, trades in by_symbol.items():
        curve = trades["r"].cumsum()
        ax.step(trades["date"], curve, where="post", color=COLORS[symbol], linewidth=2, label=symbol)
        ax.annotate(f"{symbol} {curve.iloc[-1]:+.0f}R", (trades["date"].iloc[-1], curve.iloc[-1]),
                    xytext=(6, 0), textcoords="offset points", va="center", fontsize=10, color=INK)
    ax.axhline(0, color=MUTED, linewidth=0.8)
    ax.grid(axis="y", color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.set_ylabel("Cumulative R (after slippage)", color=MUTED, fontsize=9)
    ax.set_title(title, loc="left", fontsize=12, color=INK)
    ax.legend(frameon=False, loc="lower left", fontsize=9)
    ax.margins(x=0.08)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def write(period, label, extra=""):
    data = {s: load(period, s) for s in config.SYMBOLS}
    first = min(d[1]["date"].min() for d in data.values())
    last = max(d[1]["date"].max() for d in data.values())
    st = {s: stats(*data[s]) for s in config.SYMBOLS}
    out = [f"# 10 AM model: {label} ({first:%Y-%m-%d} to {last:%Y-%m-%d})", "",
           f"Costs: ${config.SLIP_PER_SHARE:.2f}/share slippage on entry and exit, ${config.COMMISSION:.2f} commission. "
           f"Target {config.R_MULT:g}R, last entry {config.LAST_ENTRY:%H:%M}. R is per trade, after slippage.", "",
           "## Summary", "", side_by_side(st), ""]
    main = config.MAIN_SYMBOL
    for symbol in config.SYMBOLS:
        trades, _ = data[symbol]
        pct = ("win rate",)
        out += [f"## {symbol}{' (main)' if symbol == main else ''}", "",
                "By year", "", md_table(group_table(trades, trades["date"].dt.year), pct), "",
                "By weekday", "", md_table(group_table(trades, trades["date"].dt.dayofweek.map(dict(enumerate(WEEKDAYS))), WEEKDAYS), pct), "",
                "Long vs short", "", md_table(group_table(trades, "side"), pct), "",
                "By trade number of the day", "", md_table(group_table(trades, "trade_no"), pct), "",
                "By exit", "", md_table(group_table(trades, "exit_kind"), pct), "",
                "Dollars (1% risk per trade, compounding, no leverage)", ""]
        rows = [dollar_stats(trades, e) for e in config.START_EQUITIES]
        out += [md_table(pd.DataFrame(rows).set_index("start").T.rename(columns=lambda c: f"start {c}")), ""]
    out += [extra, "## Caveats", "", CAVEATS, ""]
    (RESULTS / f"report_{period}.md").write_text("\n".join(out))
    equity_chart({s: data[s][0] for s in config.SYMBOLS},
                 f"10 AM model, {label} {first:%Y}-{last:%Y}: cumulative R per trade",
                 RESULTS / f"equity_{period}.png")
    return st


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("period")
    args = parser.parse_args()
    label = {"dev": "development period", "holdout": "holdout"}.get(args.period, args.period)
    extra = ""
    if args.period == "dev":
        extra = ("## Video window\n\nNot run here. The 90 trading days before 2026-07-31 fall inside the "
                 "holdout (2024 onward), so they're reported in Phase 3 with the holdout.\n")
    write(args.period, label, extra)
    print(f"results/report_{args.period}.md, results/equity_{args.period}.png")


if __name__ == "__main__":
    main()
