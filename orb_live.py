"""Live paper-trading experiment: Opening Range Breakout on stocks in play.

Same rules as orb_stocks_in_play.py (the backtest), run for real on the paper
account so we can see actual fills:

  9:15 ET  Build today's list: stocks with 14-day average volume >= 1M shares
           and ATR > $0.50, and their average first-5-minute volume.
  9:35 ET  Rank by relative volume in the first 5 minutes, take the top 20.
           Place a buy stop at the opening range high (up bar) or a sell-short
           stop at the low (down bar).
  All day  As soon as an entry fills, place a stop-loss 10% of ATR away.
  15:55 ET Cancel anything unfilled and close every ORB position.
  Then     Report fills, slippage and profit to orb_trades.txt, orb_fills.csv
           and Discord.

Differences from the backtest:
  - With the free data plan, relative volume and the opening range come from
    IEX (one exchange) instead of the whole market. See ORB_FEED in config.py.
  - Stocks in config.SYMBOLS and anything else the account holds are skipped,
    so the experiment never touches the daily bot's shares.

    python orb_live.py             # run today (the scheduler starts it at 9:15 ET)
    python orb_live.py --dry-run   # show the picks for the last trading day, no orders
"""

import argparse
import csv
import json
import logging
import math
import re
import sys
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetAssetsRequest, GetCalendarRequest, GetOrdersRequest, MarketOrderRequest, StopOrderRequest
from dotenv import load_dotenv

import config
import notify
from bot import BOT_DIR, get_client
from orb_stocks_in_play import (LOOKBACK, MAX_LEVERAGE, MIN_ATR, MIN_AVG_VOLUME, MIN_PRICE, MIN_RELATIVE_VOLUME,
                                NOT_COMMON_STOCK, RISK_PER_TRADE, STOP_ATR_FRACTION, TOP_N, data_client, keys)

ET = ZoneInfo("America/New_York")
LIVE_DIR = BOT_DIR / "data" / "orb_live"
JOURNAL = BOT_DIR / "orb_trades.txt"
FILLS = BOT_DIR / "orb_fills.csv"
POLL_SECONDS = 5
# IEX sees a small slice of each stock's volume; ignore stocks it barely sees.
MIN_IEX_OPEN_VOLUME = 500

log = logging.getLogger("orb")


def setup_logging():
    log_dir = BOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.FileHandler(log_dir / "orb.log")]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def journal(day, text):
    heading = f"===== ORB {day:%A, %B} {day.day}, {day.year} ====="
    existing = JOURNAL.read_text() if JOURNAL.exists() else ""
    with JOURNAL.open("a") as f:
        if heading not in existing:
            f.write(("\n" if existing else "") + heading + "\n")
        f.write(f"  {datetime.now(ET):%I:%M %p} ET  {text}\n")


def alert(title, text="", kind="info"):
    notify.send(f"[ORB] {title}", text, kind=kind)


def wait_until(moment):
    while (left := (moment - datetime.now(ET)).total_seconds()) > 0:
        time.sleep(min(left, 30))


def feed():
    return DataFeed.SIP if config.ORB_FEED == "sip" else DataFeed.IEX


def bars(data, symbols, timeframe, start, end, data_feed, chunk=400):
    """Bars for many symbols, chunked, with retries."""
    frames = []
    for i in range(0, len(symbols), chunk):
        for attempt in range(5):
            try:
                df = data.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=symbols[i:i + chunk], timeframe=timeframe, start=start, end=end,
                    feed=data_feed, adjustment=Adjustment.RAW)).df
                if len(df):
                    frames.append(df.reset_index())
                break
            except Exception as e:
                log.warning(f"bars request failed ({str(e)[:80]}), retrying")
                time.sleep(10 * (attempt + 1))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def trading_days(trading, before, count):
    cal = trading.get_calendar(GetCalendarRequest(start=before - timedelta(days=count * 2 + 10), end=before - timedelta(days=1)))
    return [d.date for d in cal][-count:]


# --- 9:15: today's candidates -----------------------------------------------

def prepare(trading, data, day):
    """Stocks passing the volume/ATR filters, with their ATR and average
    first-5-minute volume over the last 14 trading days."""
    raw = TradingClient(*keys(), paper=True, raw_data=True)
    assets = raw.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE))
    held = {p.symbol for p in trading.get_all_positions()}
    skip = set(config.SYMBOLS) | held
    shortable, symbols = {}, []
    for a in assets:
        if (a["exchange"] in ("NYSE", "NASDAQ", "AMEX") and a["tradable"] and a["symbol"] not in skip
                and re.fullmatch(r"[A-Z]{1,5}", a["symbol"]) and not NOT_COMMON_STOCK.search(a["name"] or "")):
            symbols.append(a["symbol"])
            shortable[a["symbol"]] = bool(a["shortable"] and a["easy_to_borrow"])
    log.info(f"{len(symbols)} tradable common stocks (skipping {len(skip)} already held or used by the daily bot)")

    # Full-market daily bars up to yesterday (old enough for the free plan).
    start = datetime.combine(day - timedelta(days=45), dtime(0), ET)
    daily = bars(data, symbols, TimeFrame.Day, start, datetime.combine(day, dtime(0), ET), DataFeed.SIP, chunk=200)
    daily = daily.sort_values(["symbol", "timestamp"])
    stats = {}
    for symbol, g in daily.groupby("symbol"):
        if len(g) < LOOKBACK + 1:
            continue
        prev_close = g.close.shift(1)
        true_range = pd.concat([g.high - g.low, (g.high - prev_close).abs(), (g.low - prev_close).abs()], axis=1).max(axis=1)
        atr, avg_volume = true_range.tail(LOOKBACK).mean(), g.volume.tail(LOOKBACK).mean()
        if avg_volume >= MIN_AVG_VOLUME and atr > MIN_ATR and g.close.iloc[-1] > MIN_PRICE * 0.8:
            stats[symbol] = {"atr": float(atr), "shortable": shortable[symbol]}
    log.info(f"{len(stats)} pass the volume and ATR filters")

    # Average opening volume, from the same feed the 9:35 ranking will use.
    opens = []
    for d in trading_days(trading, day, LOOKBACK):
        t = datetime.combine(d, dtime(9, 30), ET)
        opens.append(bars(data, list(stats), TimeFrame(5, TimeFrameUnit.Minute), t, t + timedelta(seconds=59), feed()))
    opens = pd.concat([o for o in opens if len(o)], ignore_index=True)
    counts = opens.groupby("symbol").volume.agg(["mean", "count"])
    for symbol, row in counts.iterrows():
        if row["count"] >= 10 and row["mean"] >= MIN_IEX_OPEN_VOLUME:
            stats[symbol]["avg_open_volume"] = float(row["mean"])
    return {s: v for s, v in stats.items() if "avg_open_volume" in v}


# --- 9:35: pick the stocks in play ------------------------------------------

def pick(data, prep, day):
    t = datetime.combine(day, dtime(9, 30), ET)
    first = bars(data, list(prep), TimeFrame(5, TimeFrameUnit.Minute), t, t + timedelta(seconds=59), feed())
    candidates = []
    for row in first.itertuples():
        info = prep[row.symbol]
        direction = 1 if row.close > row.open else -1 if row.close < row.open else 0
        relative_volume = row.volume / info["avg_open_volume"]
        if (row.open > MIN_PRICE and direction and relative_volume >= MIN_RELATIVE_VOLUME
                and (direction == 1 or info["shortable"])):
            candidates.append({
                "symbol": row.symbol, "direction": direction, "relative_volume": round(relative_volume, 2),
                "trigger": round(row.high if direction == 1 else row.low, 2), "atr": info["atr"],
                "risk": STOP_ATR_FRACTION * info["atr"],
            })
    candidates.sort(key=lambda c: c["relative_volume"], reverse=True)
    picks = candidates[:TOP_N]
    for p in picks:
        per_trade_cap = config.ORB_CAPITAL * MAX_LEVERAGE / TOP_N
        p["qty"] = int(math.floor(min(config.ORB_CAPITAL * RISK_PER_TRADE / p["risk"], per_trade_cap / p["trigger"])))
    return [p for p in picks if p["qty"] >= 1]


def order_id(day, symbol, kind):
    return f"orb-{day:%Y%m%d}-{symbol}-{kind}"


def place_entries(trading, picks, day):
    for p in picks:
        side = OrderSide.BUY if p["direction"] == 1 else OrderSide.SELL
        try:
            order = trading.submit_order(StopOrderRequest(
                symbol=p["symbol"], qty=p["qty"], side=side, time_in_force=TimeInForce.DAY,
                stop_price=p["trigger"], client_order_id=order_id(day, p["symbol"], "entry")))
            p["entry_type"] = "stop"
        except APIError as e:
            # Usually: the price is already past the trigger. The backtest
            # enters at the market in that case, so do the same.
            log.info(f"{p['symbol']}: stop entry rejected ({e}), entering at market")
            try:
                order = trading.submit_order(MarketOrderRequest(
                    symbol=p["symbol"], qty=p["qty"], side=side, time_in_force=TimeInForce.DAY,
                    client_order_id=order_id(day, p["symbol"], "entry-mkt")))
                p["entry_type"] = "market (already past trigger)"
            except APIError as e2:
                log.warning(f"{p['symbol']}: entry failed: {e2}")
                p["entry_type"] = f"failed: {str(e2)[:80]}"
                continue
        p["entry_order"] = str(order.id)
        p.update(stop_order=None, stop_price=None, stop_qty=0, stop_n=0)


# --- During the day: protect each fill with a stop-loss ---------------------

def todays_orders(trading, day):
    orders = trading.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.ALL, after=datetime.combine(day, dtime(9, 0), ET), limit=500))
    return {str(o.id): o for o in orders if (o.client_order_id or "").startswith(f"orb-{day:%Y%m%d}-")}


def protect(trading, p, entry, day):
    """Place (or resize) the stop-loss for however much of the entry has filled."""
    filled = float(entry.filled_qty or 0)
    if filled <= p["stop_qty"] or not entry.filled_avg_price:
        return
    if p["stop_price"] is None:
        p["entry_fill"] = float(entry.filled_avg_price)
        p["stop_price"] = round(p["entry_fill"] - p["direction"] * p["risk"], 2)
    if p["stop_order"]:
        try:
            trading.cancel_order_by_id(p["stop_order"])
        except APIError:
            pass
    side = OrderSide.SELL if p["direction"] == 1 else OrderSide.BUY
    p["stop_n"] += 1
    try:
        order = trading.submit_order(StopOrderRequest(
            symbol=p["symbol"], qty=filled, side=side, time_in_force=TimeInForce.DAY,
            stop_price=p["stop_price"], client_order_id=order_id(day, p["symbol"], f"stop{p['stop_n']}")))
    except APIError as e:
        # The price already moved through the stop before it could be placed.
        log.info(f"{p['symbol']}: stop rejected ({e}), exiting at market")
        order = trading.submit_order(MarketOrderRequest(
            symbol=p["symbol"], qty=filled, side=side, time_in_force=TimeInForce.DAY,
            client_order_id=order_id(day, p["symbol"], f"stopmkt{p['stop_n']}")))
    p["stop_order"], p["stop_qty"] = str(order.id), filled
    log.info(f"{p['symbol']}: entry filled {filled:g} at {p['entry_fill']:.2f}, stop at {p['stop_price']:.2f}")


def monitor(trading, picks, day, until, state_path):
    while datetime.now(ET) < until:
        try:
            orders = todays_orders(trading, day)
            for p in picks:
                entry = orders.get(p.get("entry_order"))
                if entry is not None:
                    protect(trading, p, entry, day)
            save(state_path, picks)
        except Exception:
            log.exception("monitor loop error")
        time.sleep(POLL_SECONDS)


# --- 15:55: close everything and report -------------------------------------

def close_all(trading, picks, day):
    for o in todays_orders(trading, day).values():
        if o.status.value in ("new", "accepted", "partially_filled", "pending_new", "held"):
            try:
                trading.cancel_order_by_id(o.id)
            except APIError:
                pass
    time.sleep(3)
    for p in picks:
        try:
            position = trading.get_open_position(p["symbol"])
        except APIError:
            continue
        qty = abs(float(position.qty))
        side = OrderSide.SELL if float(position.qty) > 0 else OrderSide.BUY
        trading.submit_order(MarketOrderRequest(symbol=p["symbol"], qty=qty, side=side, time_in_force=TimeInForce.DAY,
                                                client_order_id=order_id(day, p["symbol"], "close")))
    # Give the closing orders a moment to fill.
    for _ in range(24):
        time.sleep(5)
        if not [o for o in todays_orders(trading, day).values() if o.client_order_id.endswith("-close") and o.status.value != "filled"]:
            break


def results(trading, picks, day):
    orders = todays_orders(trading, day).values()
    rows = []
    for p in picks:
        mine = [o for o in orders if o.client_order_id.startswith(order_id(day, p["symbol"], ""))]
        entries = [o for o in mine if "-entry" in o.client_order_id and float(o.filled_qty or 0) > 0]
        if not entries:
            continue
        entry = entries[0]
        qty, entry_price = float(entry.filled_qty), float(entry.filled_avg_price)
        exits = [o for o in mine if o not in entries and float(o.filled_qty or 0) > 0]
        exit_qty = sum(float(o.filled_qty) for o in exits)
        exit_value = sum(float(o.filled_qty) * float(o.filled_avg_price) for o in exits)
        exit_price = exit_value / exit_qty if exit_qty else float("nan")
        stopped = any("-stop" in o.client_order_id for o in exits)
        d = p["direction"]
        stop_slip = ""
        if stopped and p.get("stop_price"):
            stop_fill = next(float(o.filled_avg_price) for o in exits if "-stop" in o.client_order_id)
            stop_slip = round((p["stop_price"] - stop_fill) * d * 100, 2)
        rows.append({
            "date": str(day), "symbol": p["symbol"], "direction": "long" if d == 1 else "short",
            "relative_volume": p["relative_volume"], "entry_type": p["entry_type"], "trigger": p["trigger"],
            "entry_fill": round(entry_price, 4), "entry_slip_cents": round((entry_price - p["trigger"]) * d * 100, 2),
            "qty": qty, "stop_price": p.get("stop_price"), "exit": "stop" if stopped else "close",
            "exit_fill": round(exit_price, 4), "stop_slip_cents": stop_slip,
            "pnl": round(d * (exit_value - qty * entry_price), 2) if exit_qty else "",
        })
    return rows


def report(day, picks, rows):
    new_file = not FILLS.exists()
    if rows:
        with FILLS.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            if new_file:
                writer.writeheader()
            writer.writerows(rows)

    pnl = sum(r["pnl"] for r in rows if r["pnl"] != "")
    wins = sum(1 for r in rows if r["pnl"] != "" and r["pnl"] > 0)
    entry_slips = [r["entry_slip_cents"] for r in rows if r["entry_type"] == "stop"]
    stop_slips = [r["stop_slip_cents"] for r in rows if r["stop_slip_cents"] != ""]
    avg = lambda xs: sum(xs) / len(xs) if xs else float("nan")

    lines = [f"**Profit/loss:** {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} ({pnl / config.ORB_CAPITAL:+.2%} of ${config.ORB_CAPITAL:,})",
             f"**Trades:** {len(rows)} of {len(picks)} picks broke out  |  **Wins:** {wins}",
             f"**Slippage:** entries {avg(entry_slips):+.1f}¢/share, stops {avg(stop_slips):+.1f}¢/share "
             f"(positive = worse than the trigger; the backtest assumed 1¢)", ""]
    for r in sorted(rows, key=lambda r: r["pnl"] if r["pnl"] != "" else 0, reverse=True):
        lines.append(f"{'🟢' if r['pnl'] != '' and r['pnl'] > 0 else '🔴'} {r['symbol']} {r['direction']} {r['qty']:g} "
                     f"@ {r['entry_fill']:.2f} -> {r['exit_fill']:.2f} ({r['exit']})  {r['pnl']:+,.2f}")
        journal(day, f"{r['symbol']} {r['direction'].upper()} {r['qty']:g}: in at ${r['entry_fill']:.2f} "
                     f"(trigger ${r['trigger']:.2f}, {r['entry_slip_cents']:+.1f}c), out at ${r['exit_fill']:.2f} by {r['exit']}"
                     f"  -  {'PROFIT' if r['pnl'] != '' and r['pnl'] >= 0 else 'LOSS'} ${abs(r['pnl'] or 0):,.2f}")

    if FILLS.exists():
        history = pd.read_csv(FILLS)
        lines += ["", f"**Since the experiment started:** {history.date.nunique()} days, {len(history)} trades, "
                      f"{'+' if history.pnl.sum() >= 0 else '-'}${abs(history.pnl.sum()):,.2f}, "
                      f"average entry slippage {history.entry_slip_cents.mean():+.1f}¢"]
    journal(day, f"Day total: {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} on {len(rows)} trades")
    alert(f"End of day - {day:%a %b %d}", "\n".join(lines), kind="profit" if pnl >= 0 else "loss")


def save(path, picks):
    path.write_text(json.dumps(picks, indent=2, default=str))


# --- Main -------------------------------------------------------------------

def flatten_leftovers(trading, day):
    """If an earlier day's ORB positions are still open (e.g. the Mac slept
    through the close), close them at the open and say so."""
    earlier = sorted(p for p in LIVE_DIR.glob("*.json") if p.stem < str(day))
    if not earlier:
        return
    for p in json.loads(earlier[-1].read_text()):
        try:
            position = trading.get_open_position(p["symbol"])
        except APIError:
            continue
        qty = abs(float(position.qty))
        side = OrderSide.SELL if float(position.qty) > 0 else OrderSide.BUY
        trading.submit_order(MarketOrderRequest(symbol=p["symbol"], qty=qty, side=side, time_in_force=TimeInForce.DAY))
        alert("Closed a leftover position", f"{p['symbol']}: {qty:g} shares from {earlier[-1].stem} were still open.", kind="warning")
        journal(day, f"Closed leftover {p['symbol']} position from {earlier[-1].stem}")


def run(dry_run):
    trading = get_client()
    data = data_client()
    now = datetime.now(ET)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)

    if dry_run:
        day = trading_days(trading, now.date() + timedelta(days=1), 1)[0]
        if now.date() == day and now.time() < dtime(9, 50):
            day = trading_days(trading, now.date(), 1)[0]
        log.info(f"Dry run for {day}")
    else:
        day = now.date()
        log.info(f"Live run for {day}")
        cal = trading.get_calendar(GetCalendarRequest(start=day, end=day))
        if not cal:
            log.info("Market closed today.")
            return
        close_time = cal[0].close.replace(tzinfo=ET)
        if now.time() > dtime(9, 40):
            alert("Skipped today", f"Started at {now:%H:%M} ET, too late to trade the open (was the Mac asleep?).", kind="warning")
            return

    prep = prepare(trading, data, day)
    log.info(f"{len(prep)} candidates for {day}")

    if dry_run:
        picks = pick(data, prep, day)
        for p in picks:
            log.info(f"would {'buy' if p['direction'] == 1 else 'short'} {p['qty']} {p['symbol']} on a break of "
                     f"{p['trigger']:.2f}, stop {STOP_ATR_FRACTION:.0%} of ATR (${p['risk']:.2f}), relative volume {p['relative_volume']:.1f}x")
        print(f"{len(prep)} candidates, {len(picks)} picks for {day} (dry run, no orders)")
        for p in picks:
            print(f"  {p['symbol']:<6}{'LONG ' if p['direction'] == 1 else 'SHORT'} {p['qty']:>5} on a break of {p['trigger']:>8.2f}"
                  f"   stop ${p['risk']:.2f} away   relative volume {p['relative_volume']:.1f}x")
        return

    flatten_leftovers(trading, day)
    wait_until(datetime.combine(day, dtime(9, 35, 10), ET))
    picks = pick(data, prep, day)
    place_entries(trading, picks, day)
    state_path = LIVE_DIR / f"{day}.json"
    save(state_path, picks)
    lines = [f"{'🔼' if p['direction'] == 1 else '🔽'} **{p['symbol']}** {'long' if p['direction'] == 1 else 'short'} "
             f"{p['qty']} on a break of ${p['trigger']:.2f} (relative volume {p['relative_volume']:.1f}x)"
             + ("" if p.get("entry_type") == "stop" else f" - {p.get('entry_type')}") for p in picks]
    alert(f"{len(picks)} stocks in play", "\n".join(lines) or "No stocks qualified today.")
    journal(day, f"{len(picks)} picks placed: {', '.join(p['symbol'] for p in picks)}")

    monitor(trading, picks, day, close_time - timedelta(minutes=5), state_path)
    close_all(trading, picks, day)
    report(day, picks, results(trading, picks, day))
    save(state_path, picks)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="show the picks for the last trading day without placing orders")
    args = parser.parse_args()
    load_dotenv(BOT_DIR / ".env")
    setup_logging()
    if not config.ORB_ENABLED and not args.dry_run:
        return
    try:
        run(args.dry_run)
    except Exception:
        log.exception("ORB run failed")
        alert("ERROR", "The ORB experiment crashed, see logs/orb.log. Check Alpaca for open ORB positions.", kind="error")
        sys.exit(1)


if __name__ == "__main__":
    main()
