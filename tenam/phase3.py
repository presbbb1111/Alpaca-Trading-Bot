"""Phase 3: the holdout, run once, plus the robustness checks.

    python phase3.py --check   rehearse everything on development data only
    python phase3.py           the one holdout run (refuses if it has run before)

The real run writes results/holdout_lock.json with the time and a hash of the
code and settings, then results/report_holdout.md and equity_holdout.png.
"""

import argparse
import hashlib
import json
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, time

import numpy as np
import pandas as pd

import config
import propfirm_sim
import report
from backtest import RESULTS, run
from data import HERE, load_bars, load_calendar
from strategy import Params

LOCK = RESULTS / "holdout_lock.json"
FROZEN_FILES = ["config.py", "strategy.py", "backtest.py", "report.py", "propfirm_sim.py", "phase3.py"]
R_MULTS = [2, 3, 4, 5]
LAST_ENTRIES = [time(10, 30), time(11, 30), time(12, 30)]
MIN_STOPS = [0.0010, 0.0015, 0.0020]
RANDOM_RUNS = 200
SEED = 20240101


def file_hashes():
    return {f: hashlib.sha256((HERE / f).read_bytes()).hexdigest()[:16] for f in FROZEN_FILES}


def periods(check):
    dev = (pd.Timestamp(config.DEV_START), pd.Timestamp(config.DEV_END))
    if check:  # rehearsal: 2019-2021 plays "development", 2022-2023 plays "holdout"
        return {"dev": (dev[0], pd.Timestamp("2021-12-31")), "test": (pd.Timestamp("2022-01-01"), dev[1])}
    return {"dev": dev, "test": (pd.Timestamp(config.HOLDOUT_START), None)}


# --- parameter sensitivity ---
def sens_job(job):
    symbol, period_name, start, end, r_mult, last_entry, min_stop = job
    params = Params(r_mult=r_mult, last_entry=last_entry, min_stop_pct=min_stop)
    trades, _ = run(symbol, start, end, params=params)
    r = trades["r"]
    pf = r[r > 0].sum() / -r[r <= 0].sum() if (r <= 0).any() else float("inf")
    return {"symbol": symbol, "period": period_name, "r_mult": r_mult, "last_entry": last_entry,
            "min_stop": min_stop, "trades": len(r), "exp": r.mean(), "pf": pf}


def sensitivity(per):
    jobs = []
    for symbol in config.SYMBOLS:
        for name, (start, end) in per.items():
            for rm in R_MULTS:
                for le in LAST_ENTRIES:
                    jobs.append((symbol, name, start, end, rm, le, config.MIN_STOP_PCT))
            for ms in MIN_STOPS:
                if ms != config.MIN_STOP_PCT:
                    jobs.append((symbol, name, start, end, config.R_MULT, config.LAST_ENTRY, ms))
    with ProcessPoolExecutor(max_workers=6) as pool:
        return pd.DataFrame(list(pool.map(sens_job, jobs)))


def sens_tables(sens, symbol, name):
    s = sens[(sens["symbol"] == symbol) & (sens["period"] == name)]
    grid = s[s["min_stop"] == config.MIN_STOP_PCT]
    lines = ["| target \\ last entry | " + " | ".join(f"{t:%H:%M}" for t in LAST_ENTRIES) + " |",
             "|---|" + "---|" * len(LAST_ENTRIES)]
    for rm in R_MULTS:
        cells = []
        for le in LAST_ENTRIES:
            row = grid[(grid["r_mult"] == rm) & (grid["last_entry"] == le)].iloc[0]
            mark = "**" if (rm == config.R_MULT and le == config.LAST_ENTRY) else ""
            cells.append(f"{mark}{row['exp']:+.3f}R{mark} ({row['trades']}, PF {row['pf']:.2f})")
        lines.append(f"| {rm}R | " + " | ".join(cells) + " |")
    ms = s[(s["r_mult"] == config.R_MULT) & (s["last_entry"] == config.LAST_ENTRY)].sort_values("min_stop")
    lines += ["", "| minimum stop | expectancy | trades | profit factor |", "|---|---|---|---|"]
    for row in ms.itertuples():
        mark = "**" if row.min_stop == config.MIN_STOP_PCT else ""
        lines.append(f"| {mark}{row.min_stop:.2%}{mark} | {row.exp:+.3f}R | {row.trades} | {row.pf:.2f} |")
    return "\n".join(lines)


def neighbors(sens, symbol, name):
    s = sens[(sens["symbol"] == symbol) & (sens["period"] == name)]
    base = (s["r_mult"] == config.R_MULT) & (s["last_entry"] == config.LAST_ENTRY)
    picks = [
        (s["r_mult"] == config.R_MULT - 1) & (s["last_entry"] == config.LAST_ENTRY) & (s["min_stop"] == config.MIN_STOP_PCT),
        (s["r_mult"] == config.R_MULT + 1) & (s["last_entry"] == config.LAST_ENTRY) & (s["min_stop"] == config.MIN_STOP_PCT),
        (s["r_mult"] == config.R_MULT) & (s["last_entry"] == time(10, 30)) & (s["min_stop"] == config.MIN_STOP_PCT),
        (s["r_mult"] == config.R_MULT) & (s["last_entry"] == time(12, 30)) & (s["min_stop"] == config.MIN_STOP_PCT),
        base & (s["min_stop"] == 0.0010),
        base & (s["min_stop"] == 0.0020),
    ]
    vals = [s[p]["exp"].iloc[0] for p in picks]
    return sum(v > 0 for v in vals), len(vals)


# --- random-direction baseline ---
def day_arrays(bars):
    out = {}
    for d, g in bars.groupby("date"):
        ts = g["timestamp"]
        out[d] = (ts.dt.hour.to_numpy() * 60 + ts.dt.minute.to_numpy(), g["open"].to_numpy(),
                  g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy())
    return out


def exit_r(arr, entry_min, flat_min, sign, entry, stop, target, risk, slip):
    mins, o, h, l, c = arr
    i0 = np.searchsorted(mins, entry_min)
    mins, o, h, l, c = mins[i0:], o[i0:], h[i0:], l[i0:], c[i0:]
    if sign > 0:
        hs, ht, gap = l <= stop, h >= target, o <= stop
    else:
        hs, ht, gap = h >= stop, l <= target, o >= stop
    ev = hs | ht | (mins >= flat_min)
    k = int(np.argmax(ev)) if ev.any() else len(mins) - 1
    px = (o[k] if gap[k] else stop) if hs[k] else target if ht[k] else c[k]
    return sign * ((px - sign * slip) - (entry + sign * slip)) / risk


def both_sides(trades, bars, cal, slip=config.SLIP_PER_SHARE):
    """R for every trade taken long and taken short, same entry, risk and target size."""
    arrays = day_arrays(bars[bars["date"].isin(trades["date"].unique())])
    out = np.zeros((len(trades), 2))
    for i, t in enumerate(trades.itertuples()):
        et = pd.Timestamp(t.entry_time).tz_convert("America/New_York")
        close = cal.loc[t.date, "close"]
        flat = min(config.FLAT_TIME.hour * 60 + config.FLAT_TIME.minute, close.hour * 60 + close.minute - 5)
        sign = 1 if t.side == "long" else -1
        for j, s in enumerate((1, -1)):
            stop, target = (t.stop, t.target) if s == sign else (2 * t.entry - t.stop, 2 * t.entry - t.target)
            out[i, j] = exit_r(arrays[t.date], et.hour * 60 + et.minute, flat, s, t.entry, stop, target, t.risk, slip)
        actual = out[i, 0 if sign > 0 else 1]
        assert abs(actual - t.r) < 1e-9, f"replay mismatch on {t.date:%Y-%m-%d}: {actual} vs {t.r}"
    return out


def random_baseline(trades, bars, cal):
    sides = both_sides(trades, bars, cal)
    rng = np.random.default_rng(SEED)
    picks = rng.integers(0, 2, size=(RANDOM_RUNS, len(trades)))
    runs = sides[np.arange(len(trades)), picks].mean(axis=1)
    strat = trades["r"].mean()
    return {"strategy": strat, "random mean": runs.mean(), "random 5th pct": np.percentile(runs, 5),
            "random 95th pct": np.percentile(runs, 95), "strategy beats": (runs < strat).mean()}


# --- report sections ---
def kv_table(d, pct=()):
    lines = ["| | " + " | ".join(d) + " |", "|---|" + "---|" * len(d)]
    keys = list(next(iter(d.values())).keys())
    for k in keys:
        cells = []
        for col in d.values():
            v = col[k]
            if k in pct:
                cells.append(f"{v:.1%}")
            elif "days" in k or k == "total R":
                cells.append(f"{v:.1f}")
            elif k == "profit factor":
                cells.append(f"{v:.2f}")
            elif isinstance(v, (float, np.floating)):
                cells.append(f"{v:+.3f}" if abs(v) < 10 else f"{v:,.1f}")
            else:
                cells.append(str(v))
        lines.append(f"| {k} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def compare(stats_by):
    keep = ["trades", "win rate", "expectancy R", "total R", "profit factor", "max drawdown R"]
    return kv_table({k: {f: v[f] for f in keep} for k, v in stats_by.items()}, pct=("win rate",))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="rehearse on development data only")
    args = parser.parse_args()
    tag = "phase3check" if args.check else "holdout"
    if not args.check:
        if LOCK.exists():
            raise SystemExit(f"The holdout already ran ({json.loads(LOCK.read_text())['ran_at']}). "
                             "Its report is results/report_holdout.md.")
        LOCK.write_text(json.dumps({"ran_at": datetime.now().isoformat(timespec="seconds"),
                                    "files": file_hashes()}, indent=2))
    per = periods(args.check)
    cal = load_calendar().set_index("date")
    start, end = per["test"]
    test_trades, test_days, dev_stats, stress, rand, prop, video = {}, {}, {}, {}, {}, {}, {}
    for symbol in config.SYMBOLS:
        bars = load_bars(symbol)
        tr, dy = run(symbol, start, end, bars=bars)
        tr.to_csv(RESULTS / f"trades_{tag}_{symbol}.csv", index=False)
        dy.to_csv(RESULTS / f"days_{tag}_{symbol}.csv", index=False)
        test_trades[symbol], test_days[symbol] = report.load(tag, symbol)
        dev_stats[symbol] = report.stats(*run(symbol, *per["dev"], bars=bars))
        stress[symbol] = report.stats(*run(symbol, start, end, slip=0.02, bars=bars))
        rand[symbol] = random_baseline(test_trades[symbol], bars, cal)
        prop[symbol] = propfirm_sim.summary(propfirm_sim.simulate(test_trades[symbol], test_days[symbol]))
        if not args.check:
            vend = pd.Timestamp(config.VIDEO_WINDOW_END)
            vdays = test_days[symbol][test_days[symbol]["date"] < vend].tail(config.VIDEO_WINDOW_DAYS)
            vtr = test_trades[symbol][test_trades[symbol]["date"].isin(vdays["date"])]
            video[symbol] = (vtr, vdays)
    sens = sensitivity(per)
    sens.to_csv(RESULTS / f"sensitivity_{tag}.csv", index=False)

    test_stats = {s: report.stats(test_trades[s], test_days[s]) for s in config.SYMBOLS}
    lines = []
    lock = json.loads(LOCK.read_text()) if not args.check else {"ran_at": "rehearsal", "files": file_hashes()}
    lines += ["## Frozen settings", "",
              f"Ran {lock['ran_at']}. Minimum stop {config.MIN_STOP_PCT:.2%} of price (change 1), target "
              f"{config.R_MULT:g}R, last entry {config.LAST_ENTRY:%H:%M}, slippage ${config.SLIP_PER_SHARE:.2f}/share each way.",
              "", "File hashes (sha256, first 16): " + ", ".join(f"`{f}` {h}" for f, h in lock["files"].items()), ""]

    lines += ["## Worth continuing? (your section 4 criteria, on this period)", "",
              "| criterion | " + " | ".join(config.SYMBOLS) + " |", "|---|" + "---|" * len(config.SYMBOLS)]
    tname = "test"
    rows = {
        "expectancy >= +0.15R with >= 200 trades": lambda s: f"{test_stats[s]['expectancy R']:+.3f}R on {test_stats[s]['trades']} trades",
        "profit factor >= 1.3": lambda s: f"{test_stats[s]['profit factor']:.2f}",
        "positive years": lambda s: "{} of {}".format(*pos_count(test_trades[s], "Y")),
        "positive quarters": lambda s: "{} of {}".format(*pos_count(test_trades[s], "Q")),
        "beats >= 95% of random-direction runs": lambda s: f"{rand[s]['strategy beats']:.0%}",
        "neighboring settings positive": lambda s: "{} of {}".format(*neighbors(sens, s, tname)),
        "max drawdown": lambda s: f"{test_stats[s]['max drawdown R']:.1f}R (${-test_stats[s]['max drawdown R'] * 500:,.0f} at $500 a trade)",
    }
    for label, fn in rows.items():
        lines.append(f"| {label} | " + " | ".join(fn(s) for s in config.SYMBOLS) + " |")
    lines.append("")

    lines += ["## Development vs this period (same frozen settings)", ""]
    for s in config.SYMBOLS:
        lines += [f"{s}", "", compare({"development": dev_stats[s], "this period": test_stats[s],
                                       "this period at $0.02 slippage": stress[s]}), ""]

    lines += ["## Random-direction baseline", "",
              f"Every trade's entry time, entry price, stop distance and {config.R_MULT:g}R target kept; direction "
              f"flipped at random, {RANDOM_RUNS} runs, same exit rules and slippage. Overlaps that a one-position "
              "rule would have prevented are not removed.", "",
              kv_table(rand, pct=("strategy beats",)), ""]

    lines += ["## Parameter sensitivity", "",
              "Expectancy per trade after slippage (trades, profit factor). The frozen setting is in bold.", ""]
    for s in config.SYMBOLS:
        for name, label in (("dev", "development"), ("test", "this period")):
            lines += [f"{s}, {label}", "", sens_tables(sens, s, name), ""]

    lines += ["## Prop-firm simulation (placeholder rules)", "",
              "$50k account, pass at +$3,000, fail at $2,000 below the high-water mark (checked after each trade) "
              "or a $1,000 losing day, $500 (1%) at risk per trade so each R is $500. A new attempt starts every "
              "trading day in the period.", "",
              kv_table(prop, pct=tuple(k for k in next(iter(prop.values())) if "rate" in k or "passed" in k or "failed" in k or "unfinished" in k)), ""]

    if video:
        vstats = {s: report.stats(*video[s]) for s in config.SYMBOLS}
        first = video[config.MAIN_SYMBOL][1]["date"].min()
        lines += ["## Video window", "",
                  f"The {config.VIDEO_WINDOW_DAYS} trading days before {config.VIDEO_WINDOW_END} ({first:%Y-%m-%d} to "
                  f"{video[config.MAIN_SYMBOL][1]['date'].max():%Y-%m-%d}), next to the whole period.", ""]
        for s in config.SYMBOLS:
            lines += [s, "", compare({"video window": vstats[s], "whole period": test_stats[s]}), ""]

    label = "rehearsal on 2022-2023" if args.check else "holdout"
    report.write(tag, label, "\n".join(lines))
    print(f"results/report_{tag}.md, results/equity_{tag}.png")


def pos_count(trades, freq):
    g = trades.groupby(trades["date"].dt.to_period(freq))["r"].sum()
    return int((g > 0).sum()), len(g)


if __name__ == "__main__":
    main()
