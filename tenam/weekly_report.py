"""Compare the live paper trades with what the backtest says should have happened.

    python weekly_report.py                    last 5 days the bot ran, plus everything so far
    python weekly_report.py --days 10
    python weekly_report.py --replay           check the report on logs/trades_replay.csv
    python weekly_report.py --no-discord

The backtest runs on SIP bars (the whole market); the live bot sees IEX bars
(one exchange), so some signals differ. For each day the bot ran:
- signal match rate: backtest trades the live bot also signalled (same side,
  retest within 2 minutes), and live signals the backtest didn't have
- fill slippage: live entry fill vs the price the backtest entered at
- R difference: live R vs backtest R on matched trades
Writes results/weekly_YYYY-MM-DD.md and posts the headline to Discord.
"""

import argparse
import sys
from datetime import date

import pandas as pd

import config
import data
from backtest import RESULTS, run
from live_paper import HERE, LOGS

sys.path.append(str(HERE.parent))
import notify  # noqa: E402

MATCH_MINUTES = 2
TARGET_DAYS = 60


def load(replay):
    name = "replay" if replay else "live"
    trades_path, days_path = LOGS / f"trades_{name}.csv", LOGS / f"days_{name}.csv"
    if not days_path.exists():
        raise SystemExit(f"No {days_path.name} yet: the bot hasn't finished a day.")
    days = pd.read_csv(days_path, parse_dates=["date"])
    live = pd.read_csv(trades_path, parse_dates=["date"]) if trades_path.exists() else pd.DataFrame(columns=["date"])
    for c in ("signal_time", "entry_fill_time", "intended_entry_time"):
        if c in live:
            live[c] = pd.to_datetime(live[c], utc=True).dt.tz_convert("America/New_York")
    return days, live


def backtest_for(dates, symbol, refresh):
    if refresh:
        cal = data.download_calendar(date.today())
        data.download(symbol, cal, "sip", through=max(dates).date())  # today's bars too, once 15+ min old
    bars = data.load_bars(symbol)
    trades, _ = run(symbol, min(dates), max(dates), bars=bars)
    trades = trades[trades["date"].isin(dates)].copy()
    trades["retest_time"] = pd.to_datetime(trades["retest_time"], utc=True).dt.tz_convert("America/New_York")
    return trades


def match(live, bt):
    """Pairs (live row index, backtest row index) for the same signal."""
    pairs, used = [], set()
    for i, b in bt.iterrows():
        cands = live[(live["date"] == b["date"]) & (live["side"] == b["side"])]
        cands = cands[(cands["signal_time"] - b["retest_time"]).abs() <= pd.Timedelta(minutes=MATCH_MINUTES)]
        cands = cands[~cands.index.isin(used)]
        if len(cands):
            j = cands.index[0]
            used.add(j)
            pairs.append((j, i))
    return pairs


def section(days, live, bt):
    pairs = match(live, bt)
    lj = [j for j, _ in pairs]
    bi = [i for _, i in pairs]
    traded = live[live["live_r"].notna()]
    m = pd.DataFrame({"live_r": live.loc[lj, "live_r"].to_numpy(), "bt_r": bt.loc[bi, "r"].to_numpy(),
                      "entry_fill": live.loc[lj, "entry_fill"].to_numpy(), "bt_entry": bt.loc[bi, "entry"].to_numpy(),
                      "risk": bt.loc[bi, "risk"].to_numpy(),
                      "sign": [1 if s == "long" else -1 for s in bt.loc[bi, "side"]]})
    m = m[m["live_r"].notna()]
    slip = m["sign"] * (m["entry_fill"] - m["bt_entry"])  # positive = worse than the backtest
    reasons = live[live["status"].isin(["skipped", "missed", "not filled"])]["reason"].value_counts()
    rows = {
        "days the bot ran": len(days),
        "backtest trades (SIP)": len(bt),
        "live signals (IEX)": len(live),
        "signal match rate (backtest trades the bot also saw)": f"{len(pairs) / len(bt):.0%}" if len(bt) else "n/a",
        "live signals the backtest didn't have": len(live) - len(pairs),
        "live trades taken": len(traded),
        "live R": f"{traded['live_r'].sum():+.2f}R",
        "backtest R, same days": f"{bt['r'].sum():+.2f}R",
        "matched trades with fills": len(m),
        "avg entry fill vs backtest entry": f"${slip.mean():+.3f}/share ({(slip / m['risk']).mean():+.3f}R)" if len(m) else "n/a",
        "avg R difference (live - backtest)": f"{(m['live_r'] - m['bt_r']).mean():+.3f}R" if len(m) else "n/a",
        "avg exit slippage vs bracket price": (f"${traded['exit_slippage'].mean():+.3f}/share"
                                               if "exit_slippage" in traded and traded["exit_slippage"].notna().any() else "n/a"),
    }
    table = "| | |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows.items())
    if len(reasons):
        table += "\n\nSignals not traded: " + "; ".join(f"{r} ({n})" for r, n in reasons.items())
    return table, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()
    days, live = load(args.replay)
    bt = backtest_for(list(days["date"]), config.LIVE_SYMBOL, not args.no_refresh)
    recent = days.sort_values("date").tail(args.days)
    week_table, week = section(recent, live[live["date"].isin(recent["date"])], bt[bt["date"].isin(recent["date"])])
    all_table, _ = section(days, live, bt)
    first, last = recent["date"].min(), recent["date"].max()
    out = [f"# 10 AM model: live paper vs backtest, {first:%Y-%m-%d} to {last:%Y-%m-%d}", "",
           f"{len(days)} of {TARGET_DAYS} trading days run so far. Live bars are IEX; the backtest uses SIP.", "",
           f"## Last {len(recent)} days", "", week_table, "", "## Since the start", "", all_table, "",
           "Paper fills are kinder than real ones, especially on fast moves. Research, not financial advice.", ""]
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"weekly_{'replay_' if args.replay else ''}{last:%Y-%m-%d}.md"
    path.write_text("\n".join(out))
    print("\n".join(out))
    if not args.no_discord and not args.replay:
        notify.send(f"10 AM model: week to {last:%b %d}",
                    f"{len(days)} of {TARGET_DAYS} days run. Live {week['live R']} vs backtest "
                    f"{week['backtest R, same days']}. Signal match {week['signal match rate (backtest trades the bot also saw)']}, "
                    f"entry vs backtest {week['avg entry fill vs backtest entry']}. Full report: tenam/{path.relative_to(HERE)}")


if __name__ == "__main__":
    main()
