"""Backtest the three sleeves, $1,666.67 each, on 2019-2023 and on 2024 to now.

    python backtest.py

Writes results/report.md, results/equity.png and one trades CSV per sleeve.
The settings in config.py were picked before running this, not tuned on it.
"""

import json
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
import data  # noqa: E402
import models  # noqa: E402

RESULTS = data.HERE / "results"
PERIODS = {"2019-2023": (config.DEV_START, config.DEV_END), "2024 to now": (config.TEST_START, date.today())}


def stats(trades, equity, start_cash=config.SLEEVE):
    if equity.empty:
        return {}
    eq = equity.set_index("date")["equity"]
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365)
    total = eq.iloc[-1] / start_cash - 1
    dd = (eq / eq.cummax() - 1).min()
    out = {"trades": len(trades),
           "win rate": f"{(trades['pl'] > 0).mean():.0%}" if len(trades) else "n/a",
           "avg per trade": f"{trades['ret'].mean():+.2%}" if len(trades) else "n/a",
           "profit/loss": f"${eq.iloc[-1] - start_cash:+,.0f}",
           "return": f"{total:+.1%}",
           "a year": f"{(1 + total) ** (1 / years) - 1:+.1%}",
           "worst drop": f"{dd:.1%}"}
    if "invested" in equity:
        out["days invested"] = f"{(equity['invested'] > 0).mean():.0%}"
    return out


def hold(voo_daily, start, end, cash=config.SLEEVE):
    d = voo_daily[(voo_daily["date"] >= pd.Timestamp(start)) & (voo_daily["date"] <= pd.Timestamp(end))]
    shares = cash / d["open"].iloc[0]
    eq = pd.DataFrame({"date": d["date"], "equity": shares * d["close"], "invested": 1})
    return eq


def table(rows):
    cols = list(dict.fromkeys(k for r in rows.values() for k in r))
    lines = ["| | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for name, r in rows.items():
        lines.append(f"| {name} | " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(lines)


def by_year(trades, date_col):
    if trades.empty:
        return pd.Series(dtype=float)
    return trades.groupby(pd.to_datetime(trades[date_col]).dt.year)["pl"].sum()


def main():
    RESULTS.mkdir(exist_ok=True)
    print("Loading data...", flush=True)
    dip_data = {s: models.dip_indicators(data.load_daily(s)) for s in config.DIP_UNIVERSE
                if data.path_for("daily", s).exists()}
    voo = data.load_intraday(config.GRID_SYMBOL, 1)
    voo_daily = voo.groupby("date").agg(open=("open", "first"), close=("close", "last")).reset_index()
    stretch_data = {s: models.stretch_indicators(data.load_intraday(s, config.STRETCH_BAR_MINUTES))
                    for s in config.STRETCH_UNIVERSE if data.path_for(f"{config.STRETCH_BAR_MINUTES}min", s).exists()}
    first, last = config.DEV_START, date.today()

    print("Dip...", flush=True)
    runs = {}
    runs["dip"] = {p: models.run_dip(dip_data, a, b) for p, (a, b) in PERIODS.items()}
    dip_2x = {p: models.run_dip(dip_data, a, b, cost=2 * config.DIP_COST) for p, (a, b) in PERIODS.items()}
    print("Grid...", flush=True)
    runs["grid"] = {p: models.run_grid(voo, a, b)[:2] for p, (a, b) in PERIODS.items()}
    grid_open = {p: models.run_grid(voo, a, b)[2] for p, (a, b) in PERIODS.items()}
    step = config.GRID_STEP
    grid_steps = {}
    for s in (0.005, 0.02):
        config.GRID_STEP = config.GRID_TAKE = s
        grid_steps[s] = {p: models.run_grid(voo, a, b)[:2] for p, (a, b) in PERIODS.items()}
    config.GRID_STEP = config.GRID_TAKE = step
    print("Stretch...", flush=True)
    runs["stretch"] = {p: models.run_stretch(stretch_data, a, b) for p, (a, b) in PERIODS.items()}
    stretch_2x = {p: models.run_stretch(stretch_data, a, b, cost=2 * config.STRETCH_COST)
                  for p, (a, b) in PERIODS.items()}

    names = {"dip": "1. Dip buying (daily)", "grid": f"2. Price ladder on {config.GRID_SYMBOL}",
             "stretch": "3. Intraday stretch below VWAP"}
    out = ["# Buy low, sell high: backtest", "",
           f"$5,000 split into three sleeves of ${config.SLEEVE:,.2f}. Each is compared with simply holding "
           f"${config.SLEEVE:,.0f} of {config.GRID_SYMBOL} over the same dates. Costs are included: "
           f"{config.DIP_COST:.2%} a side on market orders; the ladder uses limit orders, filled only when the price "
           f"trades through by a cent.", ""]
    for period in PERIODS:
        a, b = PERIODS[period]
        rows = {names[k]: stats(*runs[k][period]) for k in runs}
        rows[f"Hold {config.GRID_SYMBOL}"] = stats(pd.DataFrame(columns=["pl", "ret"]), hold(voo_daily, a, b))
        rows[f"Hold {config.GRID_SYMBOL}"]["trades"] = "-"
        total_eq = sum(runs[k][period][1].set_index("date")["equity"] for k in runs).dropna().reset_index()
        rows["All three together ($5,000)"] = stats(pd.DataFrame(columns=["pl", "ret"]), total_eq, config.BUDGET)
        rows["All three together ($5,000)"]["trades"] = sum(len(runs[k][period][0]) for k in runs)
        out += [f"## {period}", "", table(rows), ""]
    out += ["## Profit/loss by year", ""]
    years = pd.DataFrame({names["dip"]: pd.concat([by_year(runs["dip"][p][0], "exit_date") for p in PERIODS]),
                          names["grid"]: pd.concat([by_year(runs["grid"][p][0], "exit_time") for p in PERIODS]),
                          names["stretch"]: pd.concat([by_year(runs["stretch"][p][0], "date") for p in PERIODS])}).fillna(0)
    voo_years = voo_daily.groupby(voo_daily["date"].dt.year).agg(o=("open", "first"), c=("close", "last"))
    years[f"Hold {config.GRID_SYMBOL} (${config.SLEEVE:,.0f} each Jan)"] = config.SLEEVE * (voo_years["c"] / voo_years["o"] - 1)
    out += ["| year | " + " | ".join(years.columns) + " |", "|---|" + "---|" * len(years.columns)]
    for y, r in years.iterrows():
        out.append(f"| {y} | " + " | ".join(f"${v:+,.0f}" for v in r) + " |")
    out += ["", "Ladder: realized profit from slices sold; slices still held are in the equity figures above.", ""]

    out += ["## Stress tests", "", "| | " + " | ".join(PERIODS) + " |", "|---|" + "---|" * len(PERIODS)]
    out.append("| Dip buying, costs doubled | " + " | ".join(stats(*dip_2x[p])["return"] for p in PERIODS) + " |")
    out.append("| Stretch, costs doubled | " + " | ".join(stats(*stretch_2x[p])["return"] for p in PERIODS) + " |")
    for s, r in grid_steps.items():
        out.append(f"| Ladder with {s:.1%} steps (vs {step:.0%}) | "
                   + " | ".join(stats(*r[p])["return"] for p in PERIODS) + " |")
    out.append("")
    for p in PERIODS:
        held = grid_open[p]
        if held:
            out.append(f"Ladder at the end of {p}: {len(held)} slices still held, bought at "
                       + ", ".join(f"${s.buy_price:,.2f}" for s in held) + ".")
    st = pd.concat([runs["stretch"][p][0] for p in PERIODS])
    if len(st):
        out += ["", "Stretch exits: " + ", ".join(f"{k} {v:.0%}" for k, v in st["exit_kind"].value_counts(normalize=True).items())
                + f". Most traded: {', '.join(st['symbol'].value_counts().head(5).index)}."]
    dp = pd.concat([runs["dip"][p][0] for p in PERIODS])
    if len(dp):
        out += [f"Dip trades hold {dp['days'].median():.0f} days (median), {dp['days'].max():.0f} at most. "
                f"Most traded: {', '.join(dp['symbol'].value_counts().head(5).index)}."]
    out += ["", "## Caveats",
            "- The stock list is today's largest companies, which flatters anything that buys dips in them "
            "(they all recovered).",
            "- 5-minute and 1-minute bars can't show the order of prices inside a bar. When a bar touches both the "
            "stop and the target, it counts as a loss.",
            "- Paper fills are kinder than real ones. Research, not financial advice.", ""]
    (RESULTS / "report.md").write_text("\n".join(out))
    keys = dict(zip(PERIODS, ("dev", "test")))
    summary = {k: {keys[p]: stats(*runs[k][p]) for p in PERIODS} for k in runs}
    summary["hold"] = {keys[p]: stats(pd.DataFrame(columns=["pl", "ret"]), hold(voo_daily, *PERIODS[p])) for p in PERIODS}
    summary["ran_at"] = pd.Timestamp.now().isoformat(timespec="seconds")
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2))
    for k in runs:
        pd.concat([runs[k][p][0].assign(period=p) for p in PERIODS]).to_csv(RESULTS / f"trades_{k}.csv", index=False)

    # Equity over the whole span, each sleeve vs holding VOO
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.2), sharey=True)
    full = {k: (models.run_dip(dip_data, first, last) if k == "dip" else
                models.run_grid(voo, first, last)[:2] if k == "grid" else
                models.run_stretch(stretch_data, first, last))[1] for k in runs}
    benchmark = hold(voo_daily, first, last)
    for ax, k in zip(axes, runs):
        ax.plot(benchmark["date"], benchmark["equity"], color="#9aa0a6", lw=1.5, label=f"Hold {config.GRID_SYMBOL}")
        ax.plot(full[k]["date"], full[k]["equity"], color="#2a6fdb", lw=2, label="Sleeve")
        ax.axvline(pd.Timestamp(config.TEST_START), color="#c0c4c8", lw=1, ls="--")
        ax.set_title(names[k], fontsize=11, loc="left")
        ax.grid(alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)
        ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
    axes[0].legend(frameon=False, loc="upper left")
    fig.suptitle(f"${config.SLEEVE:,.0f} in each sleeve, 2019 to now (dashed line: start of 2024)", fontsize=12)
    fig.tight_layout()
    fig.savefig(RESULTS / "equity.png", dpi=130)
    print("\n".join(out))


if __name__ == "__main__":
    main()
