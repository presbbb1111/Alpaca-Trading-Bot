"""Send an end-of-day account summary to Discord.

    python summary.py          # send it now
    python summary.py --print  # show it here instead of sending

Scheduled daily at 4:15 PM ET (after the stock market closes).
"""

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

import config
import notify
from bot import BOT_DIR, MARKET_TZ, get_client, load_positions, previous_close_equity, setup_logging
from strategies import STRATEGIES


def money(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def pct(x):
    return f"{x:+.2%}"


def arrow(x):
    return "🟢" if x >= 0 else "🔴"


def todays_journal():
    """Lines from today's section of trades.txt."""
    path = BOT_DIR / "trades.txt"
    if not path.exists():
        return []
    now = datetime.now(MARKET_TZ)
    heading = f"===== {now:%A, %B} {now.day}, {now.year} ====="
    text = path.read_text()
    if heading not in text:
        return []
    section = text.split(heading, 1)[1].split("=====", 1)[0]
    return [line.strip() for line in section.strip().splitlines() if line.strip()]


def bot_ran_today():
    log = BOT_DIR / "logs" / "bot.log"
    if not log.exists():
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    return any(line.startswith(today) and "Running on" in line and "dry run" not in line
               for line in log.read_text().splitlines())


def orb_summary():
    """Today's and all-time results of the ORB experiment, from orb_fills.csv."""
    path = BOT_DIR / "orb_fills.csv"
    if not path.exists():
        return "No ORB trades yet."
    fills = pd.read_csv(path)
    today = fills[fills.date == str(datetime.now(MARKET_TZ).date())]
    total = fills.pnl.sum()
    lines = []
    if len(today):
        pnl = today.pnl.sum()
        lines.append(f"{arrow(pnl)} **Today:** {money(pnl)} ({pnl / config.ORB_CAPITAL:+.2%} of ${config.ORB_CAPITAL:,}) "
                     f"on {len(today)} trades, {int((today.pnl > 0).sum())} wins")
    else:
        lines.append("No ORB trades today.")
    lines.append(f"{arrow(total)} **All time:** {money(total)} over {fills.date.nunique()} days, "
                 f"avg entry slippage {fills.entry_slip_cents.mean():+.1f}¢/share")
    return "\n".join(lines)


def benchmark_change(symbol="SPY"):
    closes = yf.Ticker(symbol).history(period="5d")["Close"].dropna()
    return closes.iloc[-1] / closes.iloc[-2] - 1


def build(client):
    account = client.get_account()
    equity, last_equity = float(account.equity), previous_close_equity(client, account)
    day_change = equity - last_equity
    total_change = equity - config.STARTING_BALANCE
    cash = float(account.cash)

    positions = {p.symbol: p for p in client.get_all_positions()}
    invested = sum(float(p.market_value) for p in positions.values())
    unrealized = sum(float(p.unrealized_pl) for p in positions.values())

    # Overview
    lines = [
        f"**Account value:** ${equity:,.2f}",
        f"{arrow(day_change)} **Today:** {money(day_change)} ({pct(day_change / last_equity)})",
        f"{arrow(total_change)} **Since start:** {money(total_change)} ({pct(total_change / config.STARTING_BALANCE)}) on ${config.STARTING_BALANCE:,}",
        f"💵 **Cash:** ${cash:,.2f}  |  **Invested:** ${invested:,.2f} ({invested / equity:.0%} of account)",
        f"📈 **Open profit/loss:** {money(unrealized)}",
    ]
    try:
        spy = benchmark_change()
        mine = day_change / last_equity
        lines.append(f"📊 **vs the market:** S&P 500 (SPY) {pct(spy)} today, you {pct(mine)} "
                     f"({'beat' if mine >= spy else 'trailed'} it by {abs(mine - spy):.2%})")
    except Exception:
        pass

    # Holdings, best first
    holdings = []
    for p in sorted(positions.values(), key=lambda p: float(p.unrealized_plpc), reverse=True):
        upl, uplpc = float(p.unrealized_pl), float(p.unrealized_plpc)
        today_pc = float(p.change_today or 0)
        qty = float(p.qty)
        holdings.append(f"{arrow(upl)} **{p.symbol}** {qty:g} @ ${float(p.current_price):,.2f} = ${float(p.market_value):,.2f}"
                        f"\n  total {money(upl)} ({pct(uplpc)})  |  today {pct(today_pc)}")

    # Profit/loss per strategy, using what each strategy paid (positions.json)
    by_strategy = defaultdict(lambda: [0.0, 0.0, 0])  # cost, value, count
    for symbol, held in load_positions().items():
        position = positions.get(symbol.replace("/", ""))
        if not position:
            continue
        price = float(position.current_price)
        for key, h in held.items():
            entry = by_strategy[STRATEGIES[key][0].split(" (")[0]]
            entry[0] += h["cost"]
            entry[1] += h["shares"] * price
            entry[2] += 1
    strategy_lines = []
    for name, (cost, value, count) in sorted(by_strategy.items(), key=lambda kv: (kv[1][1] - kv[1][0]) / kv[1][0], reverse=True):
        pl = value - cost
        strategy_lines.append(f"{arrow(pl)} **{name}**: {money(pl)} ({pct(pl / cost)}) across {count} position{'s' if count != 1 else ''}")

    # Today's activity from the journal
    journal = todays_journal()
    trades = [l.split("ET", 1)[1].strip().replace("FILL UPDATE  -  ", "").replace(" filled at ", " at ")
              for l in journal
              if re.search(r"\b(BUY|SELL)\b", l) and "FAILED" not in l and "not filled yet" not in l and "partial fill, see" not in l]
    buys = sum(" BUY " in f" {t} " for t in trades)
    sells = sum(" SELL " in f" {t} " for t in trades)
    problems = [l.split("ET", 1)[1].strip() for l in journal if "ERROR" in l or "FAILED" in l or "asleep" in l]
    realized = 0.0
    for l in journal:
        m = re.search(r"(PROFIT|LOSS) \$([\d,]+\.\d\d)", l)
        if m:
            realized += float(m.group(2).replace(",", "")) * (1 if m.group(1) == "PROFIT" else -1)

    trade_text = "\n".join(f"- {t}" for t in trades[-12:]) or "No trades today."
    if len(trades) > 12:
        trade_text = f"...{len(trades) - 12} earlier (all of them are in trades.txt)\n" + trade_text
    if trades:
        trade_text = f"**{buys} buys, {sells} sells**\n" + trade_text
    if realized:
        trade_text += f"\n**Locked-in profit/loss from sells today:** {money(realized)}"

    health = "✅ Morning run completed" if bot_ran_today() else "⚠️ The bot did NOT run today (was the Mac asleep?)"
    health += "\n" + ("\n".join(f"⚠️ {p}" for p in problems) if problems else "✅ No errors")

    fields = [
        ("Holdings (best to worst)", "\n".join(holdings) or "Nothing held.", False),
        ("Profit/loss by strategy", "\n".join(strategy_lines) or "No strategy positions.", False),
        ("Today's trades", trade_text, False),
        ("ORB experiment (day trades, separate)", orb_summary(), False),
        ("Bot health", health, False),
    ]
    title = f"End-of-day summary - {datetime.now(MARKET_TZ):%A, %b %d}"
    kind = "profit" if day_change >= 0 else "loss"
    return title, "\n".join(lines), kind, fields


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", action="store_true", help="print the summary instead of sending it")
    args = parser.parse_args()

    load_dotenv(BOT_DIR / ".env")
    setup_logging()
    title, description, kind, fields = build(get_client())
    if args.print:
        print(title, "\n", description)
        for name, value, _ in fields:
            print(f"\n[{name}]\n{value}")
        return
    if not notify.send(title, description, kind=kind, fields=fields):
        sys.exit("Summary not sent, see logs/bot.log")
    print("Summary sent to Discord.")


if __name__ == "__main__":
    main()
