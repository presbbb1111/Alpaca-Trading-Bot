"""Where each new strategy stands, for the dashboard's "New strategies" card.

Every new strategy goes through four stages:
  Backtest -> Your OK -> Paper test (PAPER_DAYS market days) -> Verdict
Progress comes from the files each one already writes (reports, logs,
journals), so nothing here needs updating by hand except a decision you make,
recorded in buylow/status.json.
"""

import json
import re
from datetime import datetime

import pandas as pd

from bot import BOT_DIR

STAGES = ["Backtest", "Your OK", "Paper test", "Verdict"]
PAPER_DAYS = 60  # market days of paper trading before judging a strategy


def journal_days(path, heading):
    """(date, day total in $) for each finished day in a bot's journal."""
    if not path.exists():
        return []
    days, current = [], None
    for line in path.read_text().splitlines():
        m = re.match(rf"===== {heading} \w+, (\w+ \d+, \d{{4}}) =====", line)
        if m:
            current = datetime.strptime(m.group(1), "%B %d, %Y").date()
        m = re.search(r"Day total: ([+-]?)\$([\d,.]+)", line)
        if m and current:
            days.append((current, float(m.group(2).replace(",", "")) * (-1 if m.group(1) == "-" else 1)))
    return days


def money(x):
    return f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"


def entry(name, started, about, stage, paper_days=0, note="", facts=(), link=None):
    """stage: index into STAGES of the stage in progress (4 = finished)."""
    return {"name": name, "started": started, "about": about, "stage": stage, "stages": STAGES,
            "paper_days": paper_days, "paper_target": PAPER_DAYS, "note": note, "facts": list(facts), "link": link}


def buylow(loaded):
    root = BOT_DIR / "buylow"
    summary_path = root / "results" / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    status_path = root / "status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    days_path = root / "logs" / "days.csv"
    days = pd.read_csv(days_path) if days_path.exists() else pd.DataFrame(columns=["date", "sleeve", "pl"])
    sleeves = [("dip", "Buy low: dip buying", "Buys a stock or ETF in an uptrend after a sharp 1-3 day drop and sells "
                                               "on the bounce, usually 2-7 days later."),
               ("grid", "Buy low: price ladder", "Buys a slice of VOO every 1% it falls and sells each slice 1% "
                                                 "above where it bought."),
               ("stretch", "Buy low: intraday stretch", "Buys a stock that has fallen far below its average price "
                                                        "for the day and sells when it gets back. Never held overnight.")]
    out = []
    for key, name, about in sleeves:
        s = summary.get(key)
        decision = status.get(key)
        mine = days[days["sleeve"] == key] if len(days) else days
        facts = []
        if s:
            facts.append(f"Backtest 2024 to now: {s['test']['a year']} a year, worst drop {s['test']['worst drop']} "
                         f"(holding VOO: {summary['hold']['test']['a year']})")
            facts.append(f"2019-2023: {s['dev']['a year']} a year over {s['dev']['trades']} trades")
        if len(mine):
            facts.append(f"Paper so far: {money(mine['pl'].sum())}")
        budget = status.get("budget", {}).get(key)
        days_run = mine["date"].nunique() if len(mine) else 0
        waiting = False
        if not s:
            stage, note = 0, "Backtest running."
        elif decision == "denied":
            stage, note = 4, "You turned it down after the backtest: it lost money every year."
        elif decision != "approved":
            stage, note = 1, "Backtest done. Waiting for your approve or deny."
        elif days_run < PAPER_DAYS:
            stage = 2
            waiting = not loaded and not days_run
            note = (f"Approved with ${budget:,}. Its schedule isn't installed yet, so it hasn't started." if waiting
                    else f"Paper trading ${budget:,}.")
        else:
            stage, note = 3, "Paper test done. Time to decide."
        e = entry(name, "Nov 16, 2026", about, stage, days_run, note, facts, "buylow/results/report.md")
        e["waiting"] = waiting
        e["denied"] = decision == "denied"
        out.append(e)
    return out


def tenam(loaded):
    days_path = BOT_DIR / "tenam" / "logs" / "days_live.csv"
    trades_path = BOT_DIR / "tenam" / "logs" / "trades_live.csv"
    days = pd.read_csv(days_path)["date"].nunique() if days_path.exists() else 0
    facts = ["Holdout 2024-26: +0.08R a trade over 747 trades. Verdict: inconclusive."]
    if trades_path.exists():
        t = pd.read_csv(trades_path)
        if "live_r" in t and t["live_r"].notna().any():
            facts.append(f"Paper so far: {t['live_r'].sum():+.2f}R on {t['live_r'].notna().sum()} trades")
    note = ("Paper test on QQQ, about $37 at risk a trade." if loaded else
            "Ready, but its schedule isn't installed yet, so it hasn't started.")
    e = entry("10 AM model (QQQ)", "Nov 15, 2026", "A 10 AM break-and-retest day trade on QQQ.",
              2 if days < PAPER_DAYS else 3, days, note, facts, "tenam/results/report_holdout.md")
    e["waiting"] = not loaded and not days
    return e


def journal_entry(name, started, about, path, heading, note):
    days = journal_days(path, heading)
    facts = ([f"Paper so far: {money(sum(pl for _, pl in days))} over {len(days)} day{'s' if len(days) != 1 else ''}"]
             if days else [])
    return entry(name, started, about, 2 if len(days) < PAPER_DAYS else 3, len(days), note, facts)


def experiments_section(loaded_jobs=()):
    items = buylow("com.presleybarr.tradingbot.buylow" in loaded_jobs)
    items.append(tenam("com.presleybarr.tradingbot.tenam" in loaded_jobs))
    items.append(journal_entry("Trades you approve", "Nov 12, 2026",
                               "Opening-range breakouts you approve on this page, sold by 3:55.",
                               BOT_DIR / "approval_trades.txt", "Approvals",
                               "Each trade needs your Approve. Results count once a day closes."))
    items.append(journal_entry("ORB experiment", "Oct 28, 2026",
                               "Day-trades opening-range breakouts on the morning's top stocks in play.",
                               BOT_DIR / "orb_trades.txt", "ORB", "Stop at 50% of ATR since Nov 6."))
    return {"items": items, "paper_days": PAPER_DAYS}
