"""Prop-firm evaluation pass/fail simulation over a list of trades.

A new attempt starts on every trading day and runs on the following days'
trades until it passes, fails, or the data runs out.

Placeholder rules (swap in a real firm's): $50k account, pass at +$3,000,
fail if equity falls $2,000 below its highest point so far (checked after
every trade), fail if a day loses $1,000 or more, 1% of the starting balance
($500) at risk per trade. Dollars are R x $500, as on a futures account where
the position size isn't limited by the account's cash.
"""

from dataclasses import dataclass

import pandas as pd


@dataclass
class Rules:
    start: float = 50_000
    profit_target: float = 3_000
    trailing_dd: float = 2_000
    daily_loss: float = 1_000
    risk_pct: float = 0.01


def attempt(day_rs, rules):
    """day_rs: list of lists of R, one list per trading day from the start day.
    Returns (outcome, trading days used)."""
    risk = rules.start * rules.risk_pct
    equity = peak = rules.start
    for n, rs in enumerate(day_rs, start=1):
        day_pnl = 0.0
        for r in rs:
            pnl = r * risk
            equity += pnl
            day_pnl += pnl
            peak = max(peak, equity)
            if equity >= rules.start + rules.profit_target:
                return "pass", n
            if equity <= peak - rules.trailing_dd:
                return "fail: trailing drawdown", n
            if day_pnl <= -rules.daily_loss:
                return "fail: daily loss", n
    return "unfinished", len(day_rs)


def simulate(trades, days, rules=None):
    rules = rules or Rules()
    by_day = trades.groupby("date")["r"].apply(list)
    day_rs = [by_day.get(d, []) for d in days["date"]]
    rows = []
    for i, d in enumerate(days["date"]):
        outcome, n = attempt(day_rs[i:], rules)
        rows.append({"start": d, "outcome": outcome, "days": n})
    return pd.DataFrame(rows)


def summary(results):
    done = results[results["outcome"] != "unfinished"]
    passed = results["outcome"] == "pass"
    failed = results["outcome"].str.startswith("fail")
    return {
        "attempts": len(results),
        "pass rate": passed.mean(),
        "fail rate": failed.mean(),
        "unfinished (data ran out)": (results["outcome"] == "unfinished").mean(),
        "of finished, passed": (done["outcome"] == "pass").mean() if len(done) else float("nan"),
        "failed on daily loss": (results["outcome"] == "fail: daily loss").mean(),
        "failed on trailing drawdown": (results["outcome"] == "fail: trailing drawdown").mean(),
        "avg trading days to pass": results.loc[passed, "days"].mean(),
        "avg trading days to fail": results.loc[failed, "days"].mean(),
    }
