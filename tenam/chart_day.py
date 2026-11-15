"""Draw one day of the 10 AM model so the logic can be checked by eye.

    python chart_day.py 2021-03-03 2022-06-14      specific days
    python chart_day.py --random 10 --seed 7       random development days

Writes charts/day_YYYY-MM-DD.png (QQQ unless --symbol says otherwise).
"""

import argparse
import random
from datetime import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
from data import HERE, load_bars, load_calendar  # noqa: E402
from strategy import median_widths, r_with_costs, run_day  # noqa: E402

CHARTS = HERE / "charts"
UP, DOWN = "#5b8c6e", "#b5574b"
RANGE = "#3b6fb6"
LONG, SHORT = "#2e7d4f", "#b03a2e"
INK, MUTED = "#1d1d1f", "#6e6e73"


def load(symbol):
    bars = load_bars(symbol)
    cal = load_calendar().set_index("date")
    return bars, median_widths(bars), cal


def run(bars, widths, cal, day):
    day = pd.Timestamp(day)
    day_bars = bars[bars["date"] == day].reset_index(drop=True)
    med = widths.get(day)
    med = None if pd.isna(med) else med
    return day_bars, run_day(day_bars, med, cal.loc[day, "close"].time())


def draw(symbol, day, day_bars, res):
    x_of = {t: i for i, t in enumerate(day_bars["timestamp"])}
    last_exit = max((x_of[t.exit_time] for t in res.trades), default=0)
    end = min(len(day_bars), max(x_of.get(day_bars["timestamp"].iloc[0] + pd.Timedelta(hours=2, minutes=30), 150), last_exit + 25))
    b = day_bars.iloc[:end]

    fig, ax = plt.subplots(figsize=(15, 7), dpi=110)
    fig.patch.set_facecolor("white")
    for i, r in b.iterrows():
        color = UP if r["close"] >= r["open"] else DOWN
        ax.vlines(i, r["low"], r["high"], color=color, linewidth=0.8)
        lo, hi = sorted((r["open"], r["close"]))
        ax.add_patch(plt.Rectangle((i - 0.35, lo), 0.7, max(hi - lo, 1e-4), color=color, linewidth=0))

    or_n = int((b["timestamp"].dt.time < config.OR_END).sum())
    if res.or_high is not None:
        ax.axvspan(-0.5, or_n - 0.5, color=RANGE, alpha=0.07, linewidth=0)
        for level, name in ((res.or_high, "OR high"), (res.or_low, "OR low")):
            ax.axhline(level, color=RANGE, linewidth=1.2)
            ax.text(end - 0.5, level, f" {name} {level:.2f}", color=RANGE, va="center", fontsize=9)

    for t, side, close in res.breaks:
        x = x_of[t] + 4  # the 5-min bar's last minute
        ax.axvspan(x_of[t] - 0.5, x + 0.5, color=LONG if side == "long" else SHORT, alpha=0.10, linewidth=0)
        y = b["high"].iloc[x] if side == "long" else b["low"].iloc[x]
        ax.annotate(f"break\n{t:%H:%M}", (x, y), xytext=(0, 18 if side == "long" else -30),
                    textcoords="offset points", ha="center", fontsize=8, color=MUTED)
    for t, side in res.retests:
        x = x_of[t]
        y = b["low"].iloc[x] if side == "long" else b["high"].iloc[x]
        ax.plot(x, y, "o", ms=9, mfc="none", mec=INK, mew=1.5)
    for t, side in res.cancels:
        ax.plot(x_of[t], b["close"].iloc[x_of[t]], "x", ms=9, color=MUTED, mew=1.5)

    for tr in res.trades:
        x0, x1 = x_of[tr.entry_time], x_of[tr.exit_time]
        color = LONG if tr.side == "long" else SHORT
        ax.hlines(tr.entry, x0, x1, color=INK, linewidth=1.5)
        ax.hlines(tr.stop, x0, x1, color=SHORT, linewidth=1.5, linestyles="--")
        ax.hlines(tr.target, x0, x1, color=LONG, linewidth=1.5, linestyles="--")
        ax.plot(x0, tr.entry, "^" if tr.side == "long" else "v", ms=11, color=color, mec="white", mew=1)
        ax.plot(x1, tr.exit_price, "s", ms=8, color=INK, mec="white", mew=1)
        ax.annotate(f"#{tr.trade_no}", (x0, tr.entry), xytext=(-6, 10 if tr.side == "long" else -16),
                    textcoords="offset points", ha="right", fontsize=9, fontweight="bold", color=color)
    if res.trades:
        lines = [
            f"#{t.trade_no} {t.side:5}  break {t.break_time:%H:%M}  retest {t.retest_time:%H:%M}  "
            f"entry {t.entry_time:%H:%M} @ {t.entry:.2f}  stop {t.stop:.2f}{' (min)' if t.stop_floored else ''}  "
            f"target {t.target:.2f}  ->  {t.exit_kind} {t.exit_time:%H:%M} @ {t.exit_price:.2f}  "
            f"{r_with_costs(t):+.2f}R"
            for t in res.trades
        ]
        ax.text(0.01, 0.98, "\n".join(lines), transform=ax.transAxes, va="top", fontsize=8.5,
                family="monospace", color=INK, bbox=dict(facecolor="white", edgecolor="#d2d2d7", alpha=0.92))

    ticks = [i for i, t in enumerate(b["timestamp"]) if t.minute % 30 == 0]
    ax.set_xticks(ticks, [b["timestamp"].iloc[i].strftime("%H:%M") for i in ticks])
    ax.set_xlim(-1, end + 12)
    ax.grid(axis="y", color="#e5e5ea", linewidth=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9)

    if res.skipped:
        status = f"skipped: {res.skipped}"
    elif not res.trades:
        status = f"no trade ({len(res.breaks)} breaks, {len(res.retests)} retests, {len(res.cancels)} cancels)"
    else:
        status = ", ".join(f"{t.side} {r_with_costs(t):+.2f}R ({t.exit_kind})" for t in res.trades)
    width = f"range {res.or_width:.2f}" if res.or_width is not None else ""
    med = f", 20-day median {res.median_width:.2f}" if res.median_width is not None else ""
    ax.set_title(f"{symbol} {day:%a %Y-%m-%d}   {width}{med}\n{status}", loc="left", fontsize=12, color=INK)
    fig.text(0.01, 0.01, "Blue band: opening range 09:30-09:59. Shaded: 5-min break bar. Circle: retest bar. "
             "Triangle: entry (next bar's open). Square: exit. Dashed: stop (red) and 4R target (green). "
             "R includes $0.01/share slippage each way. X: setup cancelled.", fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    CHARTS.mkdir(exist_ok=True)
    path = CHARTS / f"day_{day:%Y-%m-%d}.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("days", nargs="*")
    parser.add_argument("--random", type=int, default=0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--symbol", default=config.MAIN_SYMBOL)
    args = parser.parse_args()
    bars, widths, cal = load(args.symbol)
    days = [pd.Timestamp(d) for d in args.days]
    if args.random:
        dev = cal.index[(cal.index >= pd.Timestamp(config.DEV_START)) & (cal.index <= pd.Timestamp(config.DEV_END))]
        days += sorted(random.Random(args.seed).sample(list(dev), args.random))
    for day in days:
        day_bars, res = run(bars, widths, cal, day)
        print(draw(args.symbol, day, day_bars, res))


if __name__ == "__main__":
    main()
